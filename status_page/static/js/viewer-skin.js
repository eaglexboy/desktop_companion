// @ts-check
// What the viewer measures on a body when it loads, and does to accessories and outfits so they sit
// on it the way the build puts them (blender/gen_models.py, gen_imported.py, gen_accessory_models.py).
import * as THREE from "three";

/**
 * Where an accessory goes: the bone it follows, that bone's world matrix at rest, and the point.
 * @typedef {object} Frame
 * @property {THREE.Bone} bone
 * @property {THREE.Matrix4} rest
 * @property {THREE.Vector3} at
 */

/**
 * The head and neck region of the body at rest: where each vertex is (in its mesh's own space) and
 * which bones move it.
 * @typedef {object} BodySkin
 * @property {THREE.SkinnedMesh} mesh
 * @property {Float32Array} positions
 * @property {Uint16Array} index
 * @property {Float32Array} weight
 */

const HIPS = "pelvis";
const OUTFIT_KEEPS = ["head", "neck"];
const SOCKET_BONES = { head: "head", neck: "neck_01" };

/**
 * @param {THREE.Object3D | null | undefined} root
 * @returns {THREE.SkinnedMesh[]}
 */
export function skinnedMeshes(root) {
  /** @type {THREE.SkinnedMesh[]} */
  const found = [];
  root?.traverse((o) => {
    if (o instanceof THREE.SkinnedMesh) found.push(o);
  });
  return found;
}

/**
 * How high the hips stand at rest, in the scene's own units (the build's `head_local.z` of the
 * pelvis). Measured once, as the model loads: it must never be read while a clip is moving the
 * bone, or the ratio of two bodies' hips would grow with every clip played.
 * @param {THREE.Object3D} root
 */
export function hipsHeight(root) {
  root.updateMatrixWorld(true);
  const hips = root.getObjectByName(HIPS);
  return hips ? hips.getWorldPosition(new THREE.Vector3()).y : 0;
}

/**
 * What the build does to a body that wears an outfit model (`_strip_body_to_head`): every vertex
 * whose strongest bone is not the head or neck is deleted, with the faces that use it, because the
 * outfit supplies the rest (hands and feet included). Left whole, the skin shows through the cloth.
 * A body with shape keys is left alone, as in the build.
 * @param {THREE.SkinnedMesh} mesh
 */
export function stripToHead(mesh) {
  const geometry = mesh.geometry;
  const { skinIndex, skinWeight } = geometry.attributes;
  if (!geometry.index || !skinIndex || !skinWeight) return;
  if (Object.keys(geometry.morphAttributes).length) return;
  const { bones } = mesh.skeleton;
  const keep = new Uint8Array(skinIndex.count);
  for (let v = 0; v < skinIndex.count; v++) {
    let best = 0;
    let weight = -1;
    for (let c = 0; c < 4; c++) {
      if (skinWeight.getComponent(v, c) > weight) {
        weight = skinWeight.getComponent(v, c);
        best = skinIndex.getComponent(v, c);
      }
    }
    const name = (bones[best]?.name ?? "").toLowerCase();
    keep[v] =
      weight > 0 && OUTFIT_KEEPS.some((part) => name.includes(part)) ? 1 : 0;
  }
  const source = geometry.index.array;
  const groups = geometry.groups.length
    ? geometry.groups
    : [{ start: 0, count: source.length, materialIndex: 0 }];
  /** @type {number[]} */
  const kept = [];
  /** @type {[number, number, number | undefined][]} */
  const regrouped = [];
  for (const group of groups) {
    const start = kept.length;
    for (let i = group.start; i < group.start + group.count; i += 3) {
      const [a, b, c] = [source[i], source[i + 1], source[i + 2]];
      if (keep[a] && keep[b] && keep[c]) kept.push(a, b, c);
    }
    regrouped.push([start, kept.length - start, group.materialIndex]);
  }
  geometry.setIndex(kept);
  if (geometry.groups.length) {
    geometry.clearGroups();
    for (const [start, count, material] of regrouped)
      geometry.addGroup(start, count, material);
  }
}

/**
 * The body's head and neck region at rest. Hair and a beard are skinned with the nearest of these
 * vertices, so they move with the skin under them: the nape and the chin are blended between the head
 * and neck bones, and a hairstyle fixed to the head bone alone drifts away from them as it moves.
 * @param {THREE.Object3D} body
 * @param {number} aboveY  only vertices above this height are kept
 * @returns {BodySkin | null}
 */
function collectBodySkin(body, aboveY) {
  const mesh = skinnedMeshes(body).sort(
    (a, b) =>
      b.geometry.attributes.position.count -
      a.geometry.attributes.position.count,
  )[0];
  if (!mesh) return null;
  mesh.skeleton.update();
  const { skinIndex, skinWeight } = mesh.geometry.attributes;
  const point = new THREE.Vector3();
  const world = new THREE.Vector3();
  /** @type {number[]} */
  const positions = [];
  /** @type {number[]} */
  const index = [];
  /** @type {number[]} */
  const weight = [];
  for (let i = 0; i < mesh.geometry.attributes.position.count; i++) {
    mesh.getVertexPosition(i, point);
    world.copy(point).applyMatrix4(mesh.matrixWorld);
    if (world.y < aboveY) continue;
    positions.push(point.x, point.y, point.z);
    for (let c = 0; c < 4; c++) {
      index.push(skinIndex.getComponent(i, c));
      weight.push(skinWeight.getComponent(i, c));
    }
  }
  return {
    mesh,
    positions: Float32Array.from(positions),
    index: Uint16Array.from(index),
    weight: Float32Array.from(weight),
  };
}

/**
 * A copy of an accessory's meshes skinned like the body: each vertex takes the bone weights of the
 * nearest body vertex.
 * @param {THREE.Object3D} source
 * @param {THREE.Matrix4} world  where the accessory is at rest
 * @param {BodySkin} skin
 * @returns {THREE.SkinnedMesh[]}
 */
export function skinLikeBody(source, world, skin) {
  /** @type {THREE.SkinnedMesh[]} */
  const parts = [];
  source.updateMatrixWorld(true);
  const toMesh = skin.mesh.matrixWorld.clone().invert();
  const vertex = new THREE.Vector3();
  const near = skin.positions;
  source.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    const geometry = child.geometry.clone();
    geometry.applyMatrix4(
      new THREE.Matrix4().multiplyMatrices(world, child.matrixWorld),
    );
    geometry.applyMatrix4(toMesh);
    const count = geometry.attributes.position.count;
    const index = new Uint16Array(count * 4);
    const weight = new Float32Array(count * 4);
    for (let v = 0; v < count; v++) {
      vertex.fromBufferAttribute(geometry.attributes.position, v);
      let best = 0;
      let bestD = Infinity;
      for (let n = 0; n < near.length; n += 3) {
        const d =
          (near[n] - vertex.x) ** 2 +
          (near[n + 1] - vertex.y) ** 2 +
          (near[n + 2] - vertex.z) ** 2;
        if (d < bestD) {
          bestD = d;
          best = n / 3;
        }
      }
      for (let c = 0; c < 4; c++) {
        index[v * 4 + c] = skin.index[best * 4 + c];
        weight[v * 4 + c] = skin.weight[best * 4 + c];
      }
    }
    geometry.setAttribute(
      "skinIndex",
      new THREE.Uint16BufferAttribute(index, 4),
    );
    geometry.setAttribute(
      "skinWeight",
      new THREE.Float32BufferAttribute(weight, 4),
    );
    const mesh = new THREE.SkinnedMesh(geometry, child.material);
    mesh.bind(skin.mesh.skeleton, skin.mesh.bindMatrix);
    mesh.frustumCulled = false;
    parts.push(mesh);
  });
  return parts;
}

/**
 * Where accessories that are model files go on a Quaternius-style rig (the build's socket rules,
 * `imported_socket_frames`, for the two sockets drawn here): a model file's origin is its bone's own
 * point, the head bone's for the head and, for the neck, the head bone's x and z at the neck bone's
 * height. A body whose bones are named differently, or another socket, has no frame here and is left
 * to Preview. Measured once, at rest, as the model loads.
 * @param {THREE.Object3D} body
 * @param {THREE.Object3D | null} outfit
 * @returns {{ frames: Record<string, Frame>, skin: BodySkin | null }}
 */
export function restFrames(body, outfit) {
  const meshes = [...skinnedMeshes(body), ...skinnedMeshes(outfit)];
  body.updateMatrixWorld(true);
  outfit?.updateMatrixWorld(true);
  /** @type {Record<string, THREE.Bone>} */
  const bones = {};
  for (const mesh of meshes)
    for (const bone of mesh.skeleton.bones)
      bones[bone.name.toLowerCase()] ??= bone;
  const head = bones[SOCKET_BONES.head];
  if (!head) return { frames: {}, skin: null };
  const headAt = head.getWorldPosition(new THREE.Vector3());
  /** @type {Record<string, Frame>} */
  const frames = {
    socket_head_top: { bone: head, rest: head.matrixWorld.clone(), at: headAt },
  };
  const neck = bones[SOCKET_BONES.neck];
  if (neck) {
    frames.socket_neck = {
      bone: neck,
      rest: neck.matrixWorld.clone(),
      at: new THREE.Vector3(
        headAt.x,
        neck.getWorldPosition(new THREE.Vector3()).y,
        headAt.z,
      ),
    };
  }
  return { frames, skin: collectBodySkin(body, headAt.y - 0.3) };
}
