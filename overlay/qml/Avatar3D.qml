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
    property alias controller: avatarController
    // The actual QQuick3DNode the loaded model lives at (RuntimeLoader's
    // own prototype is QQuick3DNode) -- IdleBehaviors.qml animates this
    // directly (eulerRotation/scale), since View3D itself is a 2D Item
    // with no such 3D transform properties.
    property alias modelNode: loader

    // Emitted once per source change, after RuntimeLoader settles into
    // Success or Error -- Main.qml forwards this straight into an
    // asset_swap_result event back to the daemon.
    signal assetLoadResult(bool ok, string error)

    // Strips baked animation clips before RuntimeLoader sees the file: Qt
    // 6.10 auto-plays every imported animation with no way to disable it,
    // and the clips all fight over the same head bone (see glb_sanitizer.h).
    GlbSanitizer { id: glbSanitizer }

    View3D {
        id: view3d
        anchors.fill: parent

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
            position: Qt.vector3d(0, 1.0, 4.0)
            fieldOfView: 50
            clipNear: 0.1
            clipFar: 1000
        }

        RuntimeLoader {
            id: loader
            source: glbSanitizer.sanitizedSource(root.lookSource)
            onStatusChanged: {
                if (status === RuntimeLoader.Status.Success) {
                    avatarController.root = loader
                    avatarController.resetMorphWeights()
                    root.assetLoadResult(true, "")
                } else if (status === RuntimeLoader.Status.Error) {
                    // Keep running and keep the daemon connection open on
                    // a bad asset -- avatarController.root is left pointing
                    // at whatever previously loaded successfully (or null
                    // on a failed initial bootstrap), never torn down here.
                    root.assetLoadResult(false, loader.errorString)
                }
            }
        }
    }

    AvatarController {
        id: avatarController
    }
}
