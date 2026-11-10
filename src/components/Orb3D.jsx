import React, { useMemo, useRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';

// Colour + motion profile per ADA state. Everything eases between profiles,
// so state changes morph instead of snapping.
const PROFILES = {
    offline:   { a: '#475569', b: '#7f1d1d', speed: 0.15, amp: 0.04, glow: 0.35, ring: 0.15 },
    idle:      { a: '#a855f7', b: '#7c3aed', speed: 0.35, amp: 0.05, glow: 0.8,  ring: 0.35 },
    listening: { a: '#a78bfa', b: '#d946ef', speed: 0.55, amp: 0.07, glow: 1.0,  ring: 0.55 },
    thinking:  { a: '#a78bfa', b: '#22d3ee', speed: 1.4,  amp: 0.11, glow: 1.15, ring: 1.4  },
    speaking:  { a: '#c084fc', b: '#e879f9', speed: 0.9,  amp: 0.08, glow: 1.1,  ring: 0.8  },
    summoning: { a: '#f0abfc', b: '#22d3ee', speed: 1.8,  amp: 0.12, glow: 1.5,  ring: 2.0  },
    focus:     { a: '#34d399', b: '#0ea5e9', speed: 0.25, amp: 0.04, glow: 0.7,  ring: 0.25 },
    alert:     { a: '#fb7185', b: '#f59e0b', speed: 1.1,  amp: 0.11, glow: 1.4,  ring: 1.2  },
};

// 3D simplex noise (Ashima / Stefan Gustavson, MIT).
const NOISE = /* glsl */ `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z); vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`;

const shellVertex = /* glsl */ `
uniform float uTime; uniform float uAmp; uniform float uLevel;
varying vec3 vNormal; varying vec3 vView; varying float vNoise; varying vec3 vObj;
${NOISE}
void main(){
  float n = snoise(normal * 1.6 + vec3(uTime * 0.6));
  float n2 = snoise(normal * 3.0 - vec3(uTime * 0.8)) * 0.18;
  // ridged noise = the torn, fibrous plasma edge
  float ridge = pow(1.0 - abs(snoise(normal * 5.0 + vec3(uTime * 0.9))), 4.0);
  float d = (n + n2) * (uAmp + uLevel * 0.22) + ridge * (0.12 + uLevel * 0.35);
  vNoise = n; vObj = normal;
  vec3 pos = position + normal * d;
  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  vView = normalize(-mv.xyz);
  vNormal = normalize(normalMatrix * normal);
  gl_Position = projectionMatrix * mv;
}`;

const shellFragment = /* glsl */ `
uniform vec3 uColorA; uniform vec3 uColorB; uniform float uGlow; uniform float uTime;
varying vec3 vNormal; varying vec3 vView; varying float vNoise; varying vec3 vObj;
${NOISE}
void main(){
  float facing = max(dot(vNormal, vView), 0.0);
  float fres = pow(1.0 - facing, 2.2);
  vec3 col = mix(uColorA, uColorB, smoothstep(-0.6, 0.8, vNoise));
  // plasma fibres: thin bright veins crawling over the surface
  float fib = pow(1.0 - abs(snoise(vObj * 9.0 + vec3(uTime * 1.3))), 8.0)
            + pow(1.0 - abs(snoise(vObj * 17.0 - vec3(uTime))), 12.0) * 0.6;
  vec3 c = col * (0.25 + fib * 1.8) * uGlow + col * fres * 1.4 * uGlow;
  c = mix(c, vec3(1.0), pow(facing, 4.0) * 0.85 * uGlow); // white-hot centre
  gl_FragColor = vec4(c, clamp(0.35 + fib + fres * 0.5, 0.0, 1.0));
}`;

function lerpColor(target, hex, k) {
    const c = new THREE.Color(hex);
    target.lerp(c, k);
}

function Shell({ profileRef, levelRef }) {
    const mat = useRef();
    const mesh = useRef();
    const uniforms = useMemo(() => ({
        uTime: { value: 0 }, uAmp: { value: 0.1 }, uLevel: { value: 0 }, uGlow: { value: 1 },
        uColorA: { value: new THREE.Color(PROFILES.idle.a) },
        uColorB: { value: new THREE.Color(PROFILES.idle.b) },
    }), []);
    const t = useRef(0);
    useFrame((_, dt) => {
        const p = profileRef.current;
        const u = mat.current.uniforms;
        t.current += dt * p.speed;
        u.uTime.value = t.current;
        u.uAmp.value += (p.amp - u.uAmp.value) * 0.05;
        u.uLevel.value += (levelRef.current - u.uLevel.value) * 0.25;
        u.uGlow.value += (p.glow - u.uGlow.value) * 0.05;
        lerpColor(u.uColorA.value, p.a, 0.04);
        lerpColor(u.uColorB.value, p.b, 0.04);
        mesh.current.rotation.y += dt * 0.15 * p.speed;
        const s = 1 + u.uLevel.value * 0.12;
        mesh.current.scale.setScalar(s);
    });
    return (
        <mesh ref={mesh}>
            <icosahedronGeometry args={[1, 64]} />
            <shaderMaterial ref={mat} uniforms={uniforms} vertexShader={shellVertex}
                fragmentShader={shellFragment} transparent depthWrite={false} blending={THREE.AdditiveBlending} />
        </mesh>
    );
}

const coreVertex = /* glsl */ `
varying vec3 vN; varying vec3 vV;
void main(){ vec4 mv = modelViewMatrix * vec4(position,1.0); vV = normalize(-mv.xyz); vN = normalize(normalMatrix*normal); gl_Position = projectionMatrix*mv; }`;
const coreFragment = /* glsl */ `
uniform vec3 uColor; uniform float uPower;
varying vec3 vN; varying vec3 vV;
void main(){ float f = pow(max(dot(vN, vV), 0.0), 2.5); vec3 c = mix(uColor, vec3(1.0), f*0.6) * f * uPower; gl_FragColor = vec4(c, f); }`;

function Core({ profileRef, levelRef }) {
    const ref = useRef();
    const mat = useRef();
    const uniforms = useMemo(() => ({ uColor: { value: new THREE.Color(PROFILES.idle.a) }, uPower: { value: 1 } }), []);
    useFrame(({ clock }) => {
        const p = profileRef.current;
        const pulse = 0.62 + Math.sin(clock.elapsedTime * (1 + p.speed * 2)) * 0.03 + levelRef.current * 0.2;
        ref.current.scale.setScalar(pulse);
        lerpColor(mat.current.uniforms.uColor.value, p.a, 0.05);
        const u = mat.current.uniforms.uPower;
        u.value += ((0.6 + p.glow * 0.9 + levelRef.current) - u.value) * 0.08;
    });
    return (
        <mesh ref={ref}>
            <sphereGeometry args={[1, 48, 48]} />
            <shaderMaterial ref={mat} uniforms={uniforms} vertexShader={coreVertex} fragmentShader={coreFragment}
                transparent blending={THREE.AdditiveBlending} depthWrite={false} />
        </mesh>
    );
}

// Dyson lattice: a geodesic frame caged around the star. Brightens and breathes with her voice.
// Soft bloom around the star: an inside-out sphere that glows brightest at its inner edge.
const haloVertex = /* glsl */ `
varying vec3 vN; varying vec3 vV;
void main(){ vec4 mv = modelViewMatrix * vec4(position,1.0); vV = normalize(-mv.xyz); vN = normalize(normalMatrix*normal); gl_Position = projectionMatrix*mv; }`;
const haloFragment = /* glsl */ `
uniform vec3 uColor; uniform float uPower;
varying vec3 vN; varying vec3 vV;
void main(){ float f = pow(max(dot(-vN, vV), 0.0), 3.0); gl_FragColor = vec4(uColor * f * uPower, f); }`;

function Halo({ profileRef, levelRef }) {
    const ref = useRef();
    const mat = useRef();
    const uniforms = useMemo(() => ({ uColor: { value: new THREE.Color(PROFILES.idle.a) }, uPower: { value: 1 } }), []);
    useFrame(({ clock }) => {
        const p = profileRef.current;
        const l = levelRef.current;
        ref.current.scale.setScalar(1.9 + l * 0.5 + Math.sin(clock.elapsedTime * 1.5) * 0.03);
        lerpColor(mat.current.uniforms.uColor.value, p.a, 0.05);
        const u = mat.current.uniforms.uPower;
        u.value += ((0.5 + p.glow * 0.7 + l * 0.8) - u.value) * 0.08;
    });
    return (
        <mesh ref={ref}>
            <sphereGeometry args={[1, 48, 48]} />
            <shaderMaterial ref={mat} uniforms={uniforms} vertexShader={haloVertex} fragmentShader={haloFragment}
                side={THREE.BackSide} transparent blending={THREE.AdditiveBlending} depthWrite={false} />
        </mesh>
    );
}

function Lattice({ profileRef, levelRef }) {
    const group = useRef();
    const line = useRef();
    const node = useRef();
    const geo = useMemo(() => new THREE.IcosahedronGeometry(1.32, 2), []);
    useFrame((_, dt) => {
        const p = profileRef.current;
        const l = levelRef.current;
        group.current.rotation.y -= dt * 0.06 * (0.5 + p.ring);
        group.current.rotation.x += dt * 0.02;
        group.current.scale.setScalar(1 + l * 0.06);
        lerpColor(line.current.color, p.a, 0.04);
        lerpColor(node.current.color, p.b, 0.04);
        line.current.opacity += ((0.06 + l * 0.25) - line.current.opacity) * 0.15;
        node.current.size += ((0.025 + l * 0.04) - node.current.size) * 0.15;
    });
    return (
        <group ref={group}>
            <lineSegments>
                <wireframeGeometry args={[geo]} />
                <lineBasicMaterial ref={line} color={PROFILES.idle.a} transparent opacity={0.06} blending={THREE.AdditiveBlending} depthWrite={false} />
            </lineSegments>
            <points geometry={geo}>
                <pointsMaterial ref={node} size={0.035} color={PROFILES.idle.b} transparent opacity={0.9} blending={THREE.AdditiveBlending} depthWrite={false} sizeAttenuation />
            </points>
        </group>
    );
}

// Dyson swarm: rings of collector panels orbiting the star. Speech sends a wave through each ring.
const SWARM = [
    { r: 1.55, tilt: [1.25, 0.2, 0], n: 90, speed: 0.35 },
    { r: 1.7, tilt: [0.35, 1.0, 0.3], n: 100, speed: -0.28 },
    { r: 1.88, tilt: [1.6, -0.6, 0.8], n: 120, speed: 0.22 },
    { r: 2.05, tilt: [-0.5, 0.4, 1.4], n: 110, speed: -0.18 },
    { r: 1.62, tilt: [0.9, -1.2, -0.4], n: 80, speed: 0.3 },
];

function SwarmRing({ ring, profileRef, levelRef }) {
    const mesh = useRef();
    const mat = useRef();
    const dummy = useMemo(() => new THREE.Object3D(), []);
    const a = useRef(Math.random() * Math.PI * 2);
    const base = useMemo(() => new THREE.Color(), []);
    useFrame(({ clock }, dt) => {
        const p = profileRef.current;
        const l = levelRef.current;
        const t = clock.elapsedTime;
        a.current += dt * ring.speed * (0.6 + p.ring + l * 2.5);
        for (let i = 0; i < ring.n; i++) {
            const th = a.current + (i / ring.n) * Math.PI * 2;
            const lift = 1 + l * 0.14 * Math.sin(th * 6 - t * 9) + Math.sin(th * 3 + t) * 0.01;
            dummy.position.set(Math.cos(th) * ring.r * lift, Math.sin(th) * ring.r * lift, 0);
            dummy.lookAt(0, 0, 0);
            dummy.scale.setScalar(1 + l * 0.6 * Math.max(0, Math.sin(th * 6 - t * 9)));
            dummy.updateMatrix();
            mesh.current.setMatrixAt(i, dummy.matrix);
        }
        mesh.current.instanceMatrix.needsUpdate = true;
        base.set(p.a).multiplyScalar(0.4 + l * 1.4);
        mat.current.color.lerp(base, 0.2);
    });
    return (
        <instancedMesh ref={mesh} args={[null, null, ring.n]} rotation={ring.tilt}>
            <planeGeometry args={[0.07, 0.04]} />
            <meshBasicMaterial ref={mat} color={PROFILES.idle.a} side={THREE.DoubleSide} transparent opacity={0.5}
                blending={THREE.AdditiveBlending} depthWrite={false} />
        </instancedMesh>
    );
}

function Swarm(props) {
    return <group>{SWARM.map((r, i) => <SwarmRing key={i} ring={r} {...props} />)}</group>;
}

function Particles({ profileRef, levelRef, count = 900 }) {
    const pts = useRef();
    const mat = useRef();
    const positions = useMemo(() => {
        const arr = new Float32Array(count * 3);
        for (let i = 0; i < count; i++) {
            const r = 1.9 + Math.random() * 1.2;
            const th = Math.random() * Math.PI * 2;
            const ph = Math.acos(2 * Math.random() - 1);
            arr[i * 3] = r * Math.sin(ph) * Math.cos(th);
            arr[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th) * 0.55;
            arr[i * 3 + 2] = r * Math.cos(ph);
        }
        return arr;
    }, [count]);
    useFrame((_, dt) => {
        const p = profileRef.current;
        pts.current.rotation.y += dt * 0.08 * (0.5 + p.ring);
        pts.current.rotation.x = Math.sin(performance.now() / 8000) * 0.15;
        const s = 1 + levelRef.current * 0.25;
        pts.current.scale.setScalar(s);
        lerpColor(mat.current.color, p.b, 0.03);
    });
    return (
        <points ref={pts}>
            <bufferGeometry>
                <bufferAttribute attach="attributes-position" count={count} array={positions} itemSize={3} />
            </bufferGeometry>
            <pointsMaterial ref={mat} size={0.018} color={PROFILES.idle.b} transparent opacity={0.5}
                blending={THREE.AdditiveBlending} depthWrite={false} sizeAttenuation />
        </points>
    );
}

// Eases the shared voice level so glow and scale glide instead of flashing.
function LevelSmoother({ targetRef, levelRef }) {
    useFrame(() => { levelRef.current += (targetRef.current - levelRef.current) * 0.08; });
    return null;
}

// Cinematic camera: slow orbit, gentle crane and roll, pushes in while she speaks.
function CameraRig({ distRef, levelRef }) {
    const { camera } = useThree();
    const push = useRef(0);
    useFrame(({ clock }) => {
        const t = clock.elapsedTime;
        push.current += (levelRef.current - push.current) * 0.04;
        const d = distRef.current - push.current * 0.7;
        const yaw = Math.sin(t * 0.06) * 0.45;
        camera.position.x += (Math.sin(yaw) * d - camera.position.x) * 0.04;
        camera.position.z += (Math.cos(yaw) * d - camera.position.z) * 0.04;
        camera.position.y += (Math.sin(t * 0.09) * 0.55 - camera.position.y) * 0.04;
        camera.lookAt(0, 0, 0);
        camera.rotateZ(Math.sin(t * 0.05) * 0.035);
    });
    return null;
}

/**
 * Orb3D: the living core of Nexora, a Dyson sphere around a star.
 *  state  – offline | idle | listening | thinking | speaking | summoning | focus | alert
 *  level  – 0..1 voice energy (AI speech or mic), drives the surface ripple
 */
export default function Orb3D({ state = 'idle', level = 0, compact = false, className = '', style }) {
    const profileRef = useRef(PROFILES.idle);
    const levelRef = useRef(0);
    const targetRef = useRef(0);
    profileRef.current = PROFILES[state] || PROFILES.idle;
    // Raw voice level jumps every frame and made the orb strobe; LevelSmoother eases toward a softer target.
    targetRef.current = Math.max(0, Math.min(1, level)) * 0.6;
    const distRef = useRef(5.2);
    distRef.current = compact ? 6.8 : 5.2;
    return (
        <div className={className} style={style}>
            <Canvas camera={{ position: [0, 0, 5.2], fov: 45 }} dpr={[1, 2]} gl={{ antialias: true, alpha: true }}>
                <LevelSmoother targetRef={targetRef} levelRef={levelRef} />
                <CameraRig distRef={distRef} levelRef={levelRef} />
                <Particles profileRef={profileRef} levelRef={levelRef} />
                <Swarm profileRef={profileRef} levelRef={levelRef} />
                <Lattice profileRef={profileRef} levelRef={levelRef} />
                {/* the star the sphere is built around */}
                <Halo profileRef={profileRef} levelRef={levelRef} />
                <group scale={1.0}>
                    <Core profileRef={profileRef} levelRef={levelRef} />
                    <Shell profileRef={profileRef} levelRef={levelRef} />
                </group>
            </Canvas>
        </div>
    );
}
