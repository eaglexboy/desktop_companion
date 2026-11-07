// Drives viseme (lip-sync) morph-target weights on the currently loaded
// avatar model in C++, since it needs to walk the Quick3D scene graph to
// find morph targets by name -- something far more natural in C++ than QML.
// The actual weight math (attack/decay envelope, frame-rate independence,
// discontinuity handling) lives in viseme_math.{h,cpp}, kept Qt-GUI-free so
// it's unit-testable without a Quick3D compile; this class is the thin Qt
// wrapper that applies those weights to the real scene graph.
//
// NOTE on scope: idle/gesture body animation (bob, look-around, stretch,
// greet, ...) is deliberately handled at the QML level instead (see
// qml/IdleBehaviors.qml), animating the loaded model's own
// position/eulerRotation/scale directly via ordinary QML property
// animations -- NOT by replaying named armature action clips baked into
// the glTF export. Those clips are trivially previewable/reusable in a
// 3D editor or another engine, but relying on an asset-loader's named-clip
// playback API ties the client to animation metadata it would otherwise
// never need to interpret. QML-level transform animation keeps that
// dependency out of the hot path and is enough for believable idle
// liveliness.
//
// NOTE on `root`'s type: QQuick3DNode/QQuick3DModel/QQuick3DMorphTarget are
// not public C++ API in Qt6 -- only QQuick3DObject/QQuick3DGeometry/
// QQuick3DInstancing/QQuick3DTextureData/QQuick3DRenderExtension ship
// public headers; Node/Model/MorphTarget/RuntimeLoader are QML-only types
// backed by private headers. `root` is therefore a plain QObject* --
// RuntimeLoader (QtQuick3D.AssetUtils) loads an arbitrary glTF as a
// *subtree* of such QObjects, not a single Model -- and this walks that
// subtree recursively looking for anything with a `morphTargets` list
// property, applying weights via setProperty("weight", ...) rather than
// qobject_cast to a type this code is never allowed to name. That
// duck-typing is doubly necessary here, not just a style choice.
#pragma once

#include <QHash>
#include <QObject>
#include <QPointer>
#include <QQmlEngine>
#include <QStringList>
#include <QVariantList>

#include "viseme_math.h"

class AvatarController : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(QObject *root READ root WRITE setRoot NOTIFY rootChanged)
    // Shape-key names in glTF morph-target index order (the manifest's
    // `viseme_shape_keys`). RuntimeLoader's MorphTarget objects come through
    // with an EMPTY objectName, so name-based lookup can never match; the
    // i-th morph target of a model is the i-th name in this list.
    Q_PROPERTY(QStringList shapeKeyNames READ shapeKeyNames WRITE setShapeKeyNames NOTIFY shapeKeyNamesChanged)
    // The manifest's `blink_shape_keys` (the eye meshes' morph targets, e.g.
    // ["blink"]). Imported morph targets carry no names, so eye models are
    // recognised by their target COUNT matching this list; that is
    // unambiguous because it never equals the nine-strong viseme set.
    Q_PROPERTY(QStringList blinkKeyNames READ blinkKeyNames WRITE setBlinkKeyNames NOTIFY blinkKeyNamesChanged)
    // The manifest's `run_shape_keys` (["run_a", "run_b"]): the two mirrored
    // extremes of a run cycle, present on the body (and clothes) of
    // avatars with legs. Matched by target count like the blink keys.
    Q_PROPERTY(QStringList runKeyNames READ runKeyNames WRITE setRunKeyNames NOTIFY runKeyNamesChanged)
    Q_PROPERTY(QStringList gestureKeyNames READ gestureKeyNames WRITE setGestureKeyNames NOTIFY gestureKeyNamesChanged)

public:
    explicit AvatarController(QObject *parent = nullptr);

    QObject *root() const { return m_root; }
    void setRoot(QObject *root);

    QStringList shapeKeyNames() const { return m_shapeKeyNames; }
    void setShapeKeyNames(const QStringList &names);
    QStringList blinkKeyNames() const { return m_blinkKeyNames; }
    void setBlinkKeyNames(const QStringList &names);

    // Sets every eye morph target to `weight` (0 = open, 1 = closed). A
    // no-op for avatars without eye blink targets.
    Q_INVOKABLE void setBlinkWeight(double weight);
    // Whether the loaded avatar has any eye blink targets. Walks the tree,
    // so call per blink, not per frame.
    Q_INVOKABLE bool hasBlinkTargets() const;

    QStringList runKeyNames() const { return m_runKeyNames; }
    void setRunKeyNames(const QStringList &names);
    QStringList gestureKeyNames() const { return m_gestureKeyNames; }
    void setGestureKeyNames(const QStringList &names);
    // Sets the two run-pose weights (run_a, run_b) on every model that has
    // them. No-op for avatars without run targets.
    Q_INVOKABLE void setRunWeights(double a, double b);
    // Whether the loaded avatar can run (legs with run targets).
    Q_INVOKABLE bool hasRunTargets() const;

    // Arm gestures of an avatar rigged with its own arms: the generator bakes a
    // few arm poses as morph targets (`gesture_shape_keys`) on the meshes the arms
    // deform; these set their weights, in the manifest's order.
    Q_INVOKABLE void setGestureWeights(const QVariantList &weights);
    Q_INVOKABLE bool hasGestureTargets() const;

    // visemes: an array of maps with start_s/end_s/shape_key describing
    // the viseme timeline for one utterance. Call once per utterance
    // before playback starts.
    Q_INVOKABLE void setVisemeTimeline(const QVariantList &visemes);
    // Call every render tick (bound to the MediaPlayer's positionChanged,
    // converted to seconds) with elapsed time since this utterance started
    // playing. See viseme_math.h for why this is frame-rate independent,
    // drift-free, and discontinuity-safe by construction.
    Q_INVOKABLE void applyVisemeWeightsAt(double elapsedS);
    // Zeroes every currently-applied morph weight -- call when speech ends
    // or a faceless model is active.
    Q_INVOKABLE void clearVisemeWeights();
    // Drives EVERY morph target under `root` to weight 0. Qt initializes
    // imported morph targets at weight 1.0, which stacks all nine mouth
    // shapes at once (a wide black bar for a mouth). Call after every
    // load, including hot reloads -- those reuse the same loader object.
    Q_INVOKABLE void resetMorphWeights();

    // Baked glTF clips. RuntimeLoader turns every clip into a Qt Quick
    // Timeline animation object (objectName = the clip's name) and starts it
    // looping, with no API to prevent that. These helpers find those objects
    // under `root` so the overlay can stop them all after a load and then
    // play one clip at a time by name.
    // Names of the clips present (unique; per-node object clips such as
    // "idle_blink.Eye_L" are skipped).
    Q_INVOKABLE QStringList clipNames() const;
    // Stops every clip and rewinds it to frame 0 (the rest pose in our
    // generated clips). Returns how many animation objects were touched.
    Q_INVOKABLE int stopAllClips();
    // Plays clip `name` (stopping any other first): `loops` times, or
    // forever when -1. Returns false when no such clip exists. A finite
    // clip rewinds itself when done, unless `hold`: then it stays on its
    // last pose until the next clip is played (a library clip that is
    // followed straight away by another, so the body is never left in
    // the rest pose in between).
    Q_INVOKABLE bool playClip(const QString &name, int loops = 1, bool hold = false);
    // Shows the chair a body sits on (the generator's SIT_PROP_NODE) by `amount`: 1 is the
    // whole chair, 0 gone. It is a mesh with exactly three morph targets, the first of which
    // collapses it to nothing. Returns how many such meshes it found.
    Q_INVOKABLE int setSitPropShown(double amount);
    // Whether clip `name` is playing right now.
    Q_INVOKABLE bool clipRunning(const QString &name) const;
    // Duration in ms of one pass of clip `name`, or 0 if unknown.
    Q_INVOKABLE int clipDurationMs(const QString &name) const;

signals:
    void rootChanged();
    void shapeKeyNamesChanged();
    void blinkKeyNamesChanged();
    void runKeyNamesChanged();
    void gestureKeyNamesChanged();

private:
    void applyWeights(const QHash<QString, float> &desired);
    int zeroAllMorphWeights();
    QList<QObject *> clipAnimations(const QString &name = QString()) const;
    static QObject *timelineOf(QObject *anim);
    void rewindClip(QObject *anim);
    bool isBlinkModel(int targetCount) const;
    bool isRunModel(int targetCount) const;
    bool isGestureModel(int targetCount) const;
    // One mesh carrying both sets (a whole body moved by legs and arms): the run targets,
    // then the gesture targets, in that order.
    bool isRunGestureModel(int targetCount) const;
    // Models whose morph targets are not visemes (see blink/run key names).
    bool isNonVisemeModel(int targetCount) const
    {
        return isBlinkModel(targetCount) || isRunModel(targetCount) || isGestureModel(targetCount)
            || isRunGestureModel(targetCount);
    }
    void resetStep(int generation, int attempt, int passesAfterFound);

    QPointer<QObject> m_root;
    std::vector<VisemeFrame> m_timeline;
    QStringList m_lastAppliedShapeKeys;
    QStringList m_shapeKeyNames;
    QStringList m_blinkKeyNames;
    QStringList m_runKeyNames;
    QStringList m_gestureKeyNames;
    int m_resetGeneration = 0;
    int m_clipGeneration = 0;
};
