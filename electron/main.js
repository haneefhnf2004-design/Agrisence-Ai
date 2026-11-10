const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

// Use ANGLE D3D11 backend - more stable on Windows while keeping WebGL working
// This fixes "GPU state invalid after WaitForGetOffsetInRange" error
app.commandLine.appendSwitch('use-angle', 'd3d11');
app.commandLine.appendSwitch('enable-features', 'Vulkan');
app.commandLine.appendSwitch('ignore-gpu-blocklist');

let mainWindow;
// Started by Windows at login: stay hidden, voice listens, 'Hi Nexora' opens the window.
const startHidden = process.env.NEXORA_HIDDEN === '1';
let pythonProcess;
let appQuitting = false;
let backendRestarts = 0; // reset once the backend stays up for a minute

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1920,
        height: 1080,
        title: 'Nexora V2',
        webPreferences: {
            nodeIntegration: true,
            contextIsolation: false, // For simple IPC/Socket.IO usage
            backgroundThrottling: false, // keep listening for 'Hi Nexora' while hidden at login
        },
        backgroundColor: '#000000',
        frame: false, // Frameless for custom UI
        titleBarStyle: 'hidden',
        show: false, // Don't show until ready
    });

    // Links (e.g. news stories) open in the user's browser, never inside this Node-enabled window.
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (/^https?:\/\//i.test(url)) shell.openExternal(url);
        return { action: 'deny' };
    });

    // In dev, load Vite server. In prod, load index.html
    const isDev = process.env.NODE_ENV !== 'production';

    const loadFrontend = (retries = 3) => {
        const url = isDev ? 'http://localhost:5173' : null;
        const loadPromise = isDev
            ? mainWindow.loadURL(url)
            : mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));

        loadPromise
            .then(() => {
                console.log('Frontend loaded successfully!');
                windowWasShown = true;
                if (!startHidden) mainWindow.show();
                if (isDev) {
                    // DevTools disabled by default — they resize the viewport and break UI positioning
                    // Toggle with Ctrl+Shift+I if needed
                    // mainWindow.webContents.openDevTools();
                }
            })
            .catch((err) => {
                console.error(`Failed to load frontend: ${err.message}`);
                if (retries > 0) {
                    console.log(`Retrying in 1 second... (${retries} retries left)`);
                    setTimeout(() => loadFrontend(retries - 1), 1000);
                } else {
                    console.error('Failed to load frontend after all retries. Keeping window open.');
                    windowWasShown = true;
                    mainWindow.show(); // Show anyway so user sees something
                }
            });
    };

    loadFrontend();

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

function startPythonBackend() {
    const scriptPath = path.join(__dirname, '../backend/server.py');
    console.log(`Starting Python backend: ${scriptPath}`);

    // Portable: the APA_PYTHON environment variable wins, else the python on PATH.
    // (The old hardcoded C:\\Users\\WinDows\\... path only existed on one PC.)
    // On Windows prefer Python 3.12 (pyaudio and our packages live there; plain 'python' may be a newer version without them).
    let py312 = null;
    if (!process.env.APA_PYTHON && process.platform === 'win32') {
        try {
            py312 = require('child_process').execFileSync('py', ['-3.12', '-c', 'import sys;print(sys.executable)'],
                { encoding: 'utf8', timeout: 5000 }).trim() || null;
        } catch (e) { /* no py launcher or no 3.12 */ }
    }
    const pythonPath = process.env.APA_PYTHON || py312 || (process.platform === 'win32' ? 'python' : 'python3');

    pythonProcess = spawn(pythonPath, [scriptPath], {
        cwd: path.join(__dirname, '../backend'),
    });

    pythonProcess.on('error', (err) => {
        console.error(`[Python] Could not start "${pythonPath}": ${err.message}. ` +
            'Install Python 3.12 and add it to PATH, or set APA_PYTHON to python.exe.');
    });

    pythonProcess.stdout.on('data', (data) => {
        console.log(`[Python]: ${data}`);
    });

    pythonProcess.stderr.on('data', (data) => {
        console.error(`[Python Error]: ${data}`);
    });

    // Log why the backend stopped, and bring it back unless the app is closing.
    const startedAt = Date.now();
    const proc = pythonProcess;
    proc.on('exit', (code, signal) => {
        console.error(`[Python] Backend exited (code=${code}, signal=${signal}) after ${Math.round((Date.now() - startedAt) / 1000)}s`);
        if (appQuitting || pythonProcess !== proc) return;
        if (Date.now() - startedAt > 60000) backendRestarts = 0;
        if (backendRestarts >= 5) {
            console.error('[Python] Backend keeps crashing; gave up after 5 restarts. Check the log above.');
            return;
        }
        backendRestarts++;
        console.log(`[Python] Restarting backend (attempt ${backendRestarts}/5)...`);
        setTimeout(() => { if (!appQuitting) startPythonBackend(); }, 2000);
    });
}

app.whenReady().then(() => {
    ipcMain.on('window-minimize', () => {
        if (mainWindow) mainWindow.minimize();
    });

    ipcMain.on('window-maximize', () => {
        if (mainWindow) {
            if (mainWindow.isMaximized()) {
                mainWindow.unmaximize();
            } else {
                mainWindow.maximize();
            }
        }
    });

    ipcMain.on('window-show', () => {
        if (!mainWindow) return;
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();
    });

    ipcMain.on('window-close', () => {
        if (mainWindow) mainWindow.close();
    });

    checkBackendPort(8000).then((isTaken) => {
        if (isTaken) {
            console.log('Port 8000 is taken. Assuming backend is already running manually.');
            waitForBackend().then(createWindow);
        } else {
            startPythonBackend();
            // Give it a moment to start, then wait for health check
            setTimeout(() => {
                waitForBackend().then(createWindow);
            }, 1000);
        }
    });

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

function checkBackendPort(port) {
    return new Promise((resolve) => {
        const net = require('net');
        const server = net.createServer();
        server.once('error', (err) => {
            if (err.code === 'EADDRINUSE') {
                resolve(true);
            } else {
                resolve(false);
            }
        });
        server.once('listening', () => {
            server.close();
            resolve(false);
        });
        server.listen(port);
    });
}

function waitForBackend() {
    return new Promise((resolve) => {
        const check = () => {
            const http = require('http');
            http.get('http://127.0.0.1:8000/status', (res) => {
                if (res.statusCode === 200) {
                    console.log('Backend is ready!');
                    resolve();
                } else {
                    console.log('Backend not ready, retrying...');
                    setTimeout(check, 1000);
                }
            }).on('error', (err) => {
                console.log('Waiting for backend...');
                setTimeout(check, 1000);
            });
        };
        check();
    });
}

let windowWasShown = false;

app.on('window-all-closed', () => {
    // Only quit if the window was actually shown at least once
    // This prevents quitting during startup if window creation fails
    if (process.platform !== 'darwin' && windowWasShown) {
        app.quit();
    } else if (!windowWasShown) {
        console.log('Window was never shown - keeping app alive to allow retries');
    }
});

app.on('will-quit', () => {
    appQuitting = true;
    console.log('App closing... Killing Python backend.');
    if (pythonProcess) {
        if (process.platform === 'win32') {
            // Windows: Force kill the process tree synchronously
            try {
                const { execSync } = require('child_process');
                execSync(`taskkill /pid ${pythonProcess.pid} /f /t`);
            } catch (e) {
                console.error('Failed to kill python process:', e.message);
            }
        } else {
            // Unix: SIGKILL
            pythonProcess.kill('SIGKILL');
        }
        pythonProcess = null;
    }
});
