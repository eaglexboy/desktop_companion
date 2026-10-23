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

public:
    explicit AvatarController(QObject *parent = nullptr);

    QObject *root() const { return m_root; }
    void setRoot(QObject *root);

    QStringList shapeKeyNames() const { return m_shapeKeyNames; }
    void setShapeKeyNames(const QStringList &names);

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

signals:
    void rootChanged();
    void shapeKeyNamesChanged();

private:
    void applyWeights(const QHash<QString, float> &desired);
    int zeroAllMorphWeights();
    void resetStep(int generation, int attempt, int passesAfterFound);

    QPointer<QObject> m_root;
    std::vector<VisemeFrame> m_timeline;
    QStringList m_lastAppliedShapeKeys;
    QStringList m_shapeKeyNames;
    int m_resetGeneration = 0;
};
