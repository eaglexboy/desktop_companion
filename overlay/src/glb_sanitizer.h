// Qt 6.10's QtQuick3D RuntimeLoader imports every glTF animation in the
// file and starts all of them looping, with no property to turn that off.
// Our generated avatars bake ~20 head-bone clips (idle_*, think, talk_*,
// agree, ...) that all drive the same bone, so loading one verbatim makes
// the head thrash between them (a glitchy sawtooth bob that clips into the
// body). The overlay animates with QML transforms instead (see
// avatar_controller.h / IdleBehaviors.qml) and never plays baked clips, so
// it hands RuntimeLoader a copy of the file with the top-level
// "animations" array removed. Everything else -- meshes, skin, morph
// targets -- is preserved byte for byte.
#pragma once

#include <QByteArray>
#include <QObject>
#include <QQmlEngine>
#include <QString>
#include <QUrl>

class GlbSanitizer : public QObject
{
    Q_OBJECT
    QML_ELEMENT

public:
    explicit GlbSanitizer(QObject *parent = nullptr);

    // Pure: returns `glb` with the JSON chunk's "animations" removed, or an
    // empty QByteArray if `glb` is not a parseable GLB container.
    static QByteArray stripAnimations(const QByteArray &glb);

    // Maps a `file://` or `qrc:/` URL to a URL of an animation-free copy in
    // the cache directory (reused while the source is unchanged). Anything
    // that cannot be sanitized -- other schemes, a missing file, a corrupt
    // container -- is returned unchanged, so the normal RuntimeLoader error
    // path (and the daemon's asset_swap_result failure) still applies.
    Q_INVOKABLE QUrl sanitizedSource(const QUrl &source);
};
