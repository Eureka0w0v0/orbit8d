// 3D 舞台：渲染器、相机、环境光照、轨道控制器、辉光后处理与逐帧回调。

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";

const BACKGROUND = 0x0b0d12;
const CAMERA_FOV = 34;
const CAMERA_START = new THREE.Vector3(-1.55, 0.72, 2.05);
const CAMERA_TARGET = new THREE.Vector3(0, 0.02, 0);
const BLOOM = { strength: 0.85, radius: 0.5, threshold: 1.4 }; // 只让发光小球（亮度 > 1.4）泛光，白模不泛光
const ENVIRONMENT_INTENSITY = 0.45;
const DISTANCE_RINGS_M = [0.5, 1, 2, 4];
const FLOOR_Y = -0.3; // 人头模型带肩膀，肩膀底部约在耳朵下方 0.27 处
const MAX_PIXEL_RATIO = 2;

export type FrameCallback = (dt: number) => void;

export class Stage {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  readonly camera: THREE.PerspectiveCamera;
  readonly controls: OrbitControls;
  private readonly composer: EffectComposer;
  private readonly bloom: UnrealBloomPass;
  private readonly callbacks = new Set<FrameCallback>();
  private readonly timer = new THREE.Timer();
  private readonly resizeObserver: ResizeObserver;

  constructor(private readonly container: HTMLElement, ringRadius: (distM: number) => number) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, MAX_PIXEL_RATIO));
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    container.appendChild(this.renderer.domElement);

    this.scene.background = new THREE.Color(BACKGROUND);
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = ENVIRONMENT_INTENSITY;
    pmrem.dispose();

    this.camera = new THREE.PerspectiveCamera(CAMERA_FOV, 1, 0.01, 50);
    this.camera.position.copy(CAMERA_START);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(CAMERA_TARGET);
    this.controls.enableDamping = true;
    this.controls.minDistance = 0.6;
    this.controls.maxDistance = 6;
    this.controls.update();

    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(-1.5, 2.2, 2.0);
    const rim = new THREE.DirectionalLight(0x9fb4ff, 0.6);
    rim.position.set(1.8, 1.2, -2.2);
    this.scene.add(key, rim, new THREE.HemisphereLight(0xffffff, 0x20242e, 0.25));
    this.scene.add(this.buildFloor(ringRadius));

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), BLOOM.strength, BLOOM.radius, BLOOM.threshold);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    this.timer.connect(document); // 页面切到后台时不累积时间差
    this.renderer.setAnimationLoop((time) => this.frame(time));
  }

  /** 地面上的同心距离圈（0.5 / 1 / 2 / 4 m），帮助判断远近。 */
  private buildFloor(ringRadius: (distM: number) => number): THREE.Group {
    const group = new THREE.Group();
    group.position.y = FLOOR_Y;
    for (const d of DISTANCE_RINGS_M) {
      const r = ringRadius(d);
      const geo = new THREE.RingGeometry(r - 0.0015, r + 0.0015, 128);
      const mat = new THREE.MeshBasicMaterial({ color: 0x3a4152, transparent: true, opacity: 0.55, side: THREE.DoubleSide });
      const ring = new THREE.Mesh(geo, mat);
      ring.rotation.x = -Math.PI / 2;
      group.add(ring);
    }
    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(ringRadius(4) * 1.08, 96),
      new THREE.MeshBasicMaterial({ color: 0x11141b, transparent: true, opacity: 0.9 }),
    );
    disc.rotation.x = -Math.PI / 2;
    disc.position.y = -0.001;
    group.add(disc);
    return group;
  }

  onFrame(cb: FrameCallback): () => void {
    this.callbacks.add(cb);
    return () => this.callbacks.delete(cb);
  }

  resetView(): void {
    this.camera.position.copy(CAMERA_START);
    this.controls.target.copy(CAMERA_TARGET);
    this.controls.update();
  }

  private resize(): void {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (w === 0 || h === 0) return;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloom.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  private frame(time: number): void {
    this.timer.update(time);
    const dt = this.timer.getDelta();
    for (const cb of this.callbacks) cb(dt);
    this.controls.update();
    this.composer.render();
  }

  dispose(): void {
    this.renderer.setAnimationLoop(null);
    this.timer.dispose();
    this.resizeObserver.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
  }
}
