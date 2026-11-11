// Renders the currently loaded .glb through QtQuick3D.AssetUtils'
// RuntimeLoader and binds it to a Deskmate.AvatarController for viseme
// morph-weight driving. Note the import below: RuntimeLoader lives in
// QtQuick3D.AssetUtils, not QtQuick3D.Helpers.
import QtQuick
import QtQuick3D
import QtQuick3D.AssetUtils
import Deskmate

Item {
    id: root
    property string lookSource: ""
    // Hidden from the moment a look starts loading until Main.qml says its first clip is
    // playing: a freshly loaded body stands in its bind pose (a T) until then.
    property bool concealed: false
    onLookSourceChanged: {
        root.concealed = true;
        root.fit = null;
    }
    property alias controller: avatarController
    // The QQuick3DNode IdleBehaviors.qml animates (eulerRotation/scale/
    // position), since View3D itself is a 2D Item with no such 3D transform
    // properties. It is a parent of the loaded model, sitting `pivotHeight`
    // metres up, with the model offset back down by the same amount: so
    // leaning, nodding and stretching pivot about the avatar's waist
    // instead of its feet. Pivoting at the feet swung the head far toward
    // the camera on a forward lean, enlarging it past the view's edge.
    property alias modelNode: pivotNode
    property real pivotHeight: 0
    // How big the body is drawn, a fraction of its size (the look's `bounds.fit_scale`).
    property real fitScale: 1

    // Extra view height above the avatar, in px, so a turned or leaning body
    // (parts swing toward the camera and grow) is not cut off at the top. The
    // avatar keeps exactly the size and bottom edge it has at `baseHeight`:
    // the view grows upward and the camera widens to match.
    property real baseHeight: height
    readonly property real _scale: baseHeight > 0 ? height / baseHeight : 1
    // Half the visible height at the camera's distance, for the 50-degree base framing.
    readonly property real _baseHalf: 4.0 * Math.tan(25 * Math.PI / 180)

    // How far the body reaches beyond its joints on each side, in px of this item: {left, top, right,
    // bottom}, or null until it has been measured for the look that is loaded. A joint is inside the
    // body, not on its surface (the top of the head, the fingers, the belly), and how far the surface
    // is differs from model to model (a stout figure with a single bone is far wider than its joints),
    // so it is measured once per look from a picture of it.
    property var fit: null
    WindowMaskHelper {
        id: silhouette
    }
    // The box around the joints of the body as it is now: {left, top, right, bottom} in this item's
    // coordinates, or null while no model is loaded.
    function jointBox() {
        const points = avatarController.nodePositions();
        let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
        for (let i = 0; i < points.length; ++i) {
            const at = view3d.mapFrom3DScene(points[i]);
            if (!isFinite(at.x) || !isFinite(at.y))
                continue;
            left = Math.min(left, at.x);
            right = Math.max(right, at.x);
            top = Math.min(top, at.y);
            bottom = Math.max(bottom, at.y);
        }
        return left > right ? null : {
            left: left,
            top: top,
            right: right,
            bottom: bottom
        };
    }
    // Measures `fit`: how far a picture of the body reaches past the box around its joints, on each
    // side. Nothing is measured while nothing is drawn (still hidden, not yet posed); it is tried
    // again until it is.
    function measureFit() {
        const joints = jointBox();
        if (!joints)
            return;
        root.grabToImage(function (result) {
            const seen = silhouette.opaqueBounds(result.image);
            if (!seen.imageWidth || root.fit)
                return;
            const sx = root.width / seen.imageWidth;
            const sy = root.height / seen.imageHeight;
            root.fit = {
                left: Math.max(0, joints.left - seen.left * sx),
                top: Math.max(0, joints.top - seen.top * sy),
                right: Math.max(0, seen.right * sx - joints.right),
                bottom: Math.max(0, seen.bottom * sy - joints.bottom)
            };
        });
    }
    Timer {
        interval: 500
        repeat: true
        running: root.fit === null && !root.concealed && root.lookSource !== ""
        onTriggered: root.measureFit()
    }
    // The box around the body as it is now, in this item's own coordinates: {x, y, width, height}, or
    // null while there is no model or it has not been measured. It is the box around the joints,
    // widened by how far the body reaches beyond them and by `slack` times the avatar's height.
    // Main.qml makes everything outside it click-through.
    function bodyBox(slack) {
        const joints = root.fit ? jointBox() : null;
        if (!joints)
            return null;
        const room = slack * root.baseHeight;
        const left = joints.left - root.fit.left - room;
        const top = joints.top - root.fit.top - room;
        return {
            x: left,
            y: top,
            width: joints.right + root.fit.right + room - left,
            height: joints.bottom + root.fit.bottom + room - top
        };
    }

    // Emitted once per source change, after RuntimeLoader settles into
    // Success or Error -- Main.qml forwards this straight into an
    // asset_swap_result event back to the daemon.
    signal assetLoadResult(bool ok, string error)

    // Strips baked animation clips before RuntimeLoader sees the file: Qt
    // 6.10 auto-plays every imported animation with no way to disable it,
    // and the clips all fight over the same head bone (see glb_sanitizer.h).
    GlbSanitizer {
        id: glbSanitizer
    }

    View3D {
        id: view3d
        anchors.fill: parent
        opacity: root.concealed ? 0 : 1
        // Named, not left to the first camera found: `mapFrom3DScene` (bodyBox) answers 0 without it.
        camera: camera

        environment: SceneEnvironment {
            backgroundMode: SceneEnvironment.Transparent
        }

        DirectionalLight {
            eulerRotation.x: -30
            eulerRotation.y: -40
            brightness: 1.2
        }

        // Framed against a typical generated body's bounds (roughly 2-2.5m
        // tall/deep), giving the full body a comfortable margin in a
        // square view (the view is the configured avatar size, 240x240 by
        // default; the framing is proportional so it holds at any size).
        // Not asset-size-aware -- every body is
        // framed identically; revisit once bodies vary enough in scale to
        // need per-asset framing.
        //
        // clipNear is explicit and small deliberately: PerspectiveCamera's
        // default near-clip plane is too large for a ~4m camera distance
        // against meter-scale content and silently near-plane-culls the
        // whole model otherwise, even when it loaded successfully with
        // sane geometry.
        PerspectiveCamera {
            id: camera
            // y: the view's bottom edge stays at the base framing's (1.0 - half).
            position: Qt.vector3d(0, 1.0 - root._baseHalf + root._baseHalf * root._scale, 4.0)
            fieldOfView: 2 * Math.atan(Math.tan(25 * Math.PI / 180) * root._scale) * 180 / Math.PI
            clipNear: 0.1
            clipFar: 1000
        }

        // Draws the body smaller, about the spot it stands on, when its gestures would otherwise
        // leave the window (`fitScale`, measured by the generator for each look). It is a node of
        // its own because pivotNode's scale belongs to the idle animations.
        Node {
            id: fitNode
            scale: Qt.vector3d(root.fitScale, root.fitScale, root.fitScale)

            Node {
                id: pivotNode

                RuntimeLoader {
                    id: loader
                    position: Qt.vector3d(0, -root.pivotHeight, 0)
                    source: root.lookSource
                    onStatusChanged: {
                        if (status === RuntimeLoader.Status.Success) {
                            avatarController.root = loader;
                            avatarController.resetMorphWeights();
                            root.assetLoadResult(true, "");
                        } else if (status === RuntimeLoader.Status.Error) {
                            root.concealed = false;
                            // Keep running and keep the daemon connection open on
                            // a bad asset -- avatarController.root is left pointing
                            // at whatever previously loaded successfully (or null
                            // on a failed initial bootstrap), never torn down here.
                            root.assetLoadResult(false, loader.errorString);
                        }
                    }
                }
            }
        }
    }

    AvatarController {
        id: avatarController
    }
}
