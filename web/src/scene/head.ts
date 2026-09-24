// 白模人头：Lee Perry-Smith 头部扫描（CC BY 3.0），换成石膏白材质，并把两耳连线中点放到世界原点。

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

export const HEAD_MODEL_URL = "/models/LeePerrySmith.glb";

// 该模型自身坐标里的标定值（由模型顶点分析得到，见 docs/SPEC.md §10.2）
const MODEL_EAR_MID = new THREE.Vector3(-0.087, 1.504, -0.162);
const MODEL_EAR_SPAN = 3.62;
const TARGET_EAR_SPAN = 0.18; // 世界单位 ≈ 米

const CLAY = { color: 0xd8d4cc, roughness: 0.66, metalness: 0.0 };

export async function loadHead(url = HEAD_MODEL_URL): Promise<THREE.Group> {
  const gltf = await new GLTFLoader().loadAsync(url);
  let mesh: THREE.Mesh | null = null;
  gltf.scene.traverse((obj) => {
    if (!mesh && (obj as THREE.Mesh).isMesh) mesh = obj as THREE.Mesh;
  });
  if (!mesh) throw new Error("The head model contains no mesh");
  const head = mesh as THREE.Mesh;
  head.material = new THREE.MeshStandardMaterial(CLAY);
  head.geometry.computeVertexNormals();
  head.position.set(0, 0, 0);
  head.rotation.set(0, 0, 0);
  const scale = TARGET_EAR_SPAN / MODEL_EAR_SPAN;
  head.scale.setScalar(scale);
  head.position.copy(MODEL_EAR_MID).multiplyScalar(-scale);
  const group = new THREE.Group();
  group.name = "head";
  group.add(head);
  return group;
}
