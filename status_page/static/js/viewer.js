// @ts-check
// The Build page's 3D viewer (docs/ADR_BUILD_YOUR_LOOK.md, decision 4). It shows an installed
// body, the outfit model made for it, and plays the library clips the body supports, so the
// user can see how it will move without waiting for a build.
//
// The approach is the Mesh2Motion web app's: GLTFLoader, then an AnimationMixer that binds each
// clip to each skinned mesh by bone name. That works here because the Quaternius bodies and
// libraries share bone names, so a clip needs no renaming. What it matches from the build
// (blender/gen_models.py `wear_animation_library`): tracks for bones the body lacks are dropped,
// and the hips' travel is scaled by the ratio of the two rests' hip heights.
//
// It is a guide to shape and motion, not a copy of the overlay (Qt Quick 3D): lighting, glow
// and the face overlay differ, and the parametric accessories, colors and proportions need a build.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import {
  hipsHeight,
  restFrames,
  skinLikeBody,
  skinnedMeshes,
  stripToHead,
} from "./viewer-skin.js";

/**
 * @typedef {import("./viewer-skin.js").Frame} Frame
 * @typedef {import("./viewer-skin.js").BodySkin} BodySkin
 */

/**
 * An accessory that is a model file, and where it goes.
 * @typedef {object} AccessoryRequest
 * @property {string} socket
 * @property {string} url
 * @property {number[]} offset
 * @property {number} scale
 * @property {number[]} rotation
 * @property {string} skin
 */

const FIELD_OF_VIEW = 40;
const FRAME_MARGIN = 1.35;
const CLIP_FADE_S = 0.2;
// the materials of a body's own hair; mirrors brain/appearance/build_options.py HAIR_MATERIAL
const HAIR_MATERIAL = /hair|eyebrow|eyelash/i;

/**
 * @param {HTMLCanvasElement} canvas
 * @param {(status: { error: string }) => void} onStatus
 */
export function createViewer(canvas, onStatus) {
  const stage = canvas.parentElement;
  if (!stage) throw new Error("the viewer's canvas is not in the page");
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: true,
  });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(FIELD_OF_VIEW, 1, 0.01, 100);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x667788, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.position.set(2, 4, 3);
  scene.add(sun);

  const loader = new GLTFLoader();
  const mixer = new THREE.AnimationMixer(new THREE.Object3D());
  /** @type {Map<string, { clips: THREE.AnimationClip[], hips: number }>} */
  const libraries = new Map();
  /** @type {THREE.Object3D | null} */
  let body = null;
  let bodyHips = 0; // the body's hips at rest
  /** @type {THREE.Object3D | null} */
  let outfit = null;
  /** @type {Record<string, Frame>} */
  let frames = {}; // where accessories go on the shown body
  /** @type {BodySkin | null} */
  let bodySkin = null; // the body's head and neck weights, for skinned accessories
  /** @type {THREE.Object3D[]} */
  let accessories = [];
  let accessoryJob = 0;
  /** @type {THREE.AnimationAction[]} */
  let actions = [];
  /** @type {THREE.AnimationClip | null} */
  let current = null; // the clip being played, so its cached bindings can be dropped
  let hairColor = "#ffffff";
  let hairstyleColor = "#ffffff";
  let paused = false;
  let active = false;
  let frame = 0;
  let token = 0; // a load that finishes after a newer one started is dropped
  const clock = new THREE.Clock();

  function resize() {
    const w = stage?.clientWidth ?? 0;
    const h = stage?.clientHeight ?? 0;
    if (!w || !h) return;
    renderer.setSize(w, h, false); // false: the CSS owns the element's size
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(stage);

  function framing() {
    if (!body) return;
    const box = new THREE.Box3().setFromObject(body);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const fit =
      (Math.max(size.y, size.x) * FRAME_MARGIN) /
      2 /
      Math.tan((FIELD_OF_VIEW * Math.PI) / 360);
    controls.target.copy(center);
    camera.position.set(center.x, center.y, center.z + fit);
    controls.update();
  }

  /**
   * Shows `bodyUrl` (and `outfitUrl` on it, if given). Returns true when the body is on show.
   * @param {string | null} bodyUrl
   * @param {string | null} [outfitUrl]
   */
  async function setModel(bodyUrl, outfitUrl = null) {
    const mine = ++token;
    stop();
    body?.removeFromParent();
    outfit?.removeFromParent();
    body = outfit = null;
    frames = {};
    bodySkin = null;
    accessories = [];
    if (!bodyUrl) return false;
    try {
      const [shownBody, shownOutfit] = await Promise.all([
        loader.loadAsync(bodyUrl),
        outfitUrl ? loader.loadAsync(outfitUrl) : null,
      ]);
      if (mine !== token) return false;
      body = shownBody.scene;
      bodyHips = hipsHeight(body);
      scene.add(body);
      tintHair();
      if (shownOutfit) {
        outfit = shownOutfit.scene;
        scene.add(outfit);
        skinnedMeshes(body).forEach(stripToHead);
      }
      ({ frames, skin: bodySkin } = restFrames(body, outfit));
      framing();
      return true;
    } catch (error) {
      if (mine === token)
        onStatus({
          error: error instanceof Error ? error.message : String(error),
        });
      return false;
    }
  }

  /** Tints the body's hair materials (eyebrows, eyelashes); white shows them as modelled. */
  function tintHair() {
    body?.traverse((object) => {
      if (!(object instanceof THREE.Mesh)) return;
      for (const material of [object.material].flat())
        if (HAIR_MATERIAL.test(material.name) && material.color)
          material.color.set(hairColor);
    });
  }

  /**
   * A hair or beard model's texture is near black, which tinting cannot lighten, so any color but
   * white replaces the texture (as the build does); white shows the model as it was made.
   * @param {THREE.MeshStandardMaterial} material
   * @param {string} color
   */
  function flatColor(material, color) {
    material.userData.map ??= material.map;
    const white = color.toLowerCase() === "#ffffff";
    material.map = white ? material.userData.map : null;
    material.color.set(white ? "#ffffff" : color);
    material.needsUpdate = true;
  }

  /** Colors the hair and beard models on the body. */
  function tintHairstyles() {
    for (const item of accessories)
      item.traverse((object) => {
        if (!(object instanceof THREE.Mesh)) return;
        for (const material of [object.material].flat())
          if (HAIR_MATERIAL.test(material.name) && material.color)
            flatColor(material, hairstyleColor);
      });
  }

  /** Every skinned mesh of the shown body and outfit. */
  const shownMeshes = () => [...skinnedMeshes(body), ...skinnedMeshes(outfit)];

  /**
   * @param {string} url
   * @returns {Promise<{ clips: THREE.AnimationClip[], hips: number }>}
   */
  async function libraryClips(url) {
    let library = libraries.get(url);
    if (!library) {
      const gltf = await loader.loadAsync(url);
      library = { clips: gltf.animations, hips: hipsHeight(gltf.scene) };
      libraries.set(url, library);
    }
    return library;
  }

  /**
   * Plays `clipName` of the library at `url` on the body and outfit. Returns how many of the
   * clip's tracks were dropped for bones the body lacks.
   * @param {string} url
   * @param {string} clipName
   * @param {number} [speed]
   */
  async function play(url, clipName, speed = 1) {
    if (!body) return 0;
    const mine = token;
    const library = await libraryClips(url);
    if (mine !== token || !body) return 0;
    const source = library.clips.find((c) => c.name === clipName);
    if (!source) return 0;
    const bones = new Set(
      shownMeshes().flatMap((mesh) => mesh.skeleton.bones.map((b) => b.name)),
    );
    const ratio =
      library.hips > 1e-6 && bodyHips > 1e-6 ? bodyHips / library.hips : 1;
    /** @type {THREE.KeyframeTrack[]} */
    const tracks = [];
    let dropped = 0;
    for (const track of source.tracks) {
      if (!bones.has(track.name.split(".")[0])) {
        dropped += 1; // a bone this body lacks, as in the build
        continue;
      }
      const t = track.clone();
      if (t.name.endsWith(".position"))
        t.values = t.values.map((v) => v * ratio);
      tracks.push(t);
    }
    stop();
    current = new THREE.AnimationClip(source.name, source.duration, tracks);
    for (const mesh of shownMeshes()) {
      const action = mixer.clipAction(current, mesh); // bound by bone name, per skinned mesh
      action.reset().setEffectiveTimeScale(speed).fadeIn(CLIP_FADE_S).play();
      actions.push(action);
    }
    return dropped;
  }

  /**
   * Shows the accessories that are model files, each fixed to its socket's bone (or skinned like the
   * body) so it moves with the clip. Returns the sockets drawn: one with no frame here, or a rotated
   * placement, is not drawn and is left to Preview.
   * @param {AccessoryRequest[]} list
   */
  async function setAccessories(list) {
    const job = ++accessoryJob;
    for (const item of accessories) item.removeFromParent();
    accessories = [];
    /** @type {string[]} */
    const shown = [];
    for (const entry of list) {
      const place = frames[entry.socket];
      const turned = entry.rotation.some((r) => Math.abs(r) > 1e-6);
      if (!place || turned || !body) continue;
      /** @type {import("three/addons/loaders/GLTFLoader.js").GLTF} */
      let gltf;
      try {
        gltf = await loader.loadAsync(entry.url);
      } catch {
        continue;
      }
      if (job !== accessoryJob || !body) return shown;
      const [ox, oy, oz] = entry.offset;
      // the build's axes are Blender's: (x, y, z) there is (x, z, -y) here
      const world = new THREE.Matrix4().compose(
        place.at.clone().add(new THREE.Vector3(ox, oz, -oy)),
        new THREE.Quaternion(),
        new THREE.Vector3().setScalar(entry.scale),
      );
      const host = bodySkin?.mesh.parent;
      if (entry.skin === "body" && bodySkin && host) {
        // skinned like the body: it moves with the skin, not rigidly with one bone
        for (const part of skinLikeBody(gltf.scene, world, bodySkin)) {
          part.matrixAutoUpdate = false;
          part.matrix.copy(bodySkin.mesh.matrix);
          host.add(part);
          accessories.push(part);
        }
      } else {
        const item = gltf.scene;
        item.matrix.copy(place.rest.clone().invert().multiply(world));
        item.matrix.decompose(item.position, item.quaternion, item.scale);
        place.bone.add(item);
        accessories.push(item);
      }
      shown.push(entry.socket);
    }
    tintHairstyles();
    return shown;
  }

  function stop() {
    for (const action of actions) action.stop();
    actions = [];
    if (current) mixer.uncacheClip(current);
    current = null;
  }

  function tick() {
    frame = 0;
    if (!active) return;
    const dt = clock.getDelta();
    if (!paused) mixer.update(dt);
    controls.update();
    renderer.render(scene, camera);
    frame = requestAnimationFrame(tick);
  }

  return {
    setModel,
    setAccessories,
    play,
    stop,
    /** @param {string} color */
    setHairColor(color) {
      hairColor = color;
      tintHair();
    },
    /** @param {string} color */
    setHairstyleColor(color) {
      hairstyleColor = color;
      tintHairstyles();
    },
    /** @param {boolean} value */
    setPaused(value) {
      paused = value;
    },
    /** Only draws while the Build page is on show. @param {boolean} value */
    setActive(value) {
      active = value;
      if (value) {
        resize();
        clock.getDelta();
        if (!frame) frame = requestAnimationFrame(tick);
      }
    },
    isPaused: () => paused,
    hasBody: () => !!body,
  };
}
