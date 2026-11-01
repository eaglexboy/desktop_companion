#include "avatar_controller.h"

#include <QQmlListReference>
#include <QTimer>

AvatarController::AvatarController(QObject *parent) : QObject(parent) {}

void AvatarController::setRoot(QObject *root)
{
    if (m_root == root)
        return;
    m_root = root;
    emit rootChanged();
}

void AvatarController::setShapeKeyNames(const QStringList &names)
{
    if (m_shapeKeyNames == names)
        return;
    m_shapeKeyNames = names;
    emit shapeKeyNamesChanged();
}

void AvatarController::setBlinkKeyNames(const QStringList &names)
{
    if (m_blinkKeyNames == names)
        return;
    m_blinkKeyNames = names;
    emit blinkKeyNamesChanged();
}

bool AvatarController::isBlinkModel(int targetCount) const
{
    return !m_blinkKeyNames.isEmpty() && targetCount == m_blinkKeyNames.size()
        && targetCount != m_shapeKeyNames.size();
}

void AvatarController::setRunKeyNames(const QStringList &names)
{
    if (m_runKeyNames == names)
        return;
    m_runKeyNames = names;
    emit runKeyNamesChanged();
}

bool AvatarController::isRunModel(int targetCount) const
{
    return !m_runKeyNames.isEmpty() && targetCount == m_runKeyNames.size()
        && targetCount != m_shapeKeyNames.size() && targetCount != m_blinkKeyNames.size();
}

bool AvatarController::hasRunTargets() const
{
    if (!m_root || m_runKeyNames.isEmpty())
        return false;
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        QQmlListReference ref(node, "morphTargets");
        if (ref.isValid() && isRunModel(int(ref.count())))
            return true;
        stack.append(node->children());
    }
    return false;
}

void AvatarController::setRunWeights(double a, double b)
{
    if (!m_root || m_runKeyNames.size() != 2)
        return;
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        QQmlListReference ref(node, "morphTargets");
        if (ref.isValid() && isRunModel(int(ref.count()))) {
            if (QObject *t = ref.at(0))
                t->setProperty("weight", float(a));
            if (QObject *t = ref.at(1))
                t->setProperty("weight", float(b));
        }
        stack.append(node->children());
    }
}

bool AvatarController::hasBlinkTargets() const
{
    if (!m_root || m_blinkKeyNames.isEmpty())
        return false;
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        QQmlListReference ref(node, "morphTargets");
        if (ref.isValid() && isBlinkModel(int(ref.count())))
            return true;
        stack.append(node->children());
    }
    return false;
}

void AvatarController::setBlinkWeight(double weight)
{
    if (!m_root || m_blinkKeyNames.isEmpty())
        return;
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        QQmlListReference ref(node, "morphTargets");
        if (ref.isValid() && isBlinkModel(int(ref.count()))) {
            for (qsizetype i = 0; i < ref.count(); ++i) {
                if (QObject *target = ref.at(i))
                    target->setProperty("weight", float(weight));
            }
        }
        stack.append(node->children());
    }
}

int AvatarController::zeroAllMorphWeights()
{
    if (!m_root)
        return 0;
    int found = 0;
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        QQmlListReference ref(node, "morphTargets");
        if (ref.isValid()) {
            for (qsizetype i = 0; i < ref.count(); ++i) {
                if (QObject *target = ref.at(i)) {
                    target->setProperty("weight", 0.0f);
                    ++found;
                }
            }
        }
        stack.append(node->children());
    }
    return found;
}

void AvatarController::resetMorphWeights()
{
    m_lastAppliedShapeKeys.clear();
    resetStep(++m_resetGeneration, 0, 0);
}

// RuntimeLoader may still be instantiating scene objects when its status
// flips to Success (and a hot reload creates a fresh set after that), so a
// single pass is not enough. Keep zeroing every 100ms until morph targets
// have actually been found, then a few more passes to catch late ones.
// A newer reset (e.g. another hot reload) supersedes this one.
void AvatarController::resetStep(int generation, int attempt, int passesAfterFound)
{
    if (generation != m_resetGeneration)
        return;
    const int found = zeroAllMorphWeights();
    // New clip objects may appear late too, each starting out looping.
    stopAllClips();
    const bool done = (found > 0 && passesAfterFound >= 5) || attempt >= 50;
    if (done)
        return;
    QTimer::singleShot(100, this, [this, generation, attempt, found, passesAfterFound]() {
        resetStep(generation, attempt + 1, found > 0 ? passesAfterFound + 1 : 0);
    });
}

// QQuickTimelineAnimation / QQuickTimeline are private types, so they are
// found by class name and driven through their properties.
QList<QObject *> AvatarController::clipAnimations(const QString &name) const
{
    QList<QObject *> found;
    if (!m_root)
        return found;
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        if (!qstrcmp(node->metaObject()->className(), "QQuickTimelineAnimation")
            && (name.isEmpty() || node->objectName() == name))
            found.append(node);
        stack.append(node->children());
    }
    return found;
}

QStringList AvatarController::clipNames() const
{
    QStringList names;
    for (QObject *anim : clipAnimations()) {
        const QString n = anim->objectName();
        if (!n.isEmpty() && !n.contains(QLatin1Char('.')) && !names.contains(n))
            names << n;
    }
    return names;
}

QObject *AvatarController::timelineOf(QObject *anim)
{
    QObject *parent = anim->parent();
    return parent && !qstrcmp(parent->metaObject()->className(), "QQuickTimeline") ? parent : nullptr;
}

void AvatarController::rewindClip(QObject *anim)
{
    anim->setProperty("running", false);
    // The animation is a child of the Timeline it drives (its `timeline`
    // property is not readable as a QObject* from here).
    if (QObject *timeline = timelineOf(anim))
        timeline->setProperty("currentFrame", anim->property("from"));
}

int AvatarController::stopAllClips()
{
    ++m_clipGeneration;
    const QList<QObject *> anims = clipAnimations();
    for (QObject *anim : anims)
        rewindClip(anim);
    return int(anims.size());
}

int AvatarController::clipDurationMs(const QString &name) const
{
    const QList<QObject *> anims = clipAnimations(name);
    return anims.isEmpty() ? 0 : anims.first()->property("duration").toInt();
}

bool AvatarController::playClip(const QString &name, int loops)
{
    const QList<QObject *> anims = clipAnimations(name);
    if (anims.isEmpty())
        return false;
    stopAllClips();
    const int generation = m_clipGeneration;
    for (QObject *anim : anims) {
        anim->setProperty("loops", loops);
        anim->setProperty("running", true);
    }
    if (loops > 0) {
        const int totalMs = clipDurationMs(name) * loops + 50;
        QTimer::singleShot(totalMs, this, [this, generation, name]() {
            // Only if nothing else has been played/stopped since.
            if (generation != m_clipGeneration)
                return;
            for (QObject *anim : clipAnimations(name))
                rewindClip(anim);
        });
    }
    return true;
}

void AvatarController::setVisemeTimeline(const QVariantList &visemes)
{
    m_timeline.clear();
    m_timeline.reserve(visemes.size());
    for (const QVariant &v : visemes) {
        const QVariantMap m = v.toMap();
        VisemeFrame frame;
        frame.startS = m.value(QStringLiteral("start_s")).toDouble();
        frame.endS = m.value(QStringLiteral("end_s")).toDouble();
        frame.shapeKey = m.value(QStringLiteral("shape_key")).toString();
        m_timeline.push_back(frame);
    }
}

void AvatarController::applyVisemeWeightsAt(double elapsedS)
{
    const std::vector<VisemeWeight> active = VisemeMath::weightsAt(m_timeline, elapsedS);

    QHash<QString, float> desired;
    QStringList stillActive;
    for (const VisemeWeight &w : active) {
        desired.insert(w.shapeKey, w.weight);
        stillActive << w.shapeKey;
    }
    // Anything that was applied last tick but isn't active this tick must
    // be explicitly driven to 0 -- otherwise it would stay stuck at
    // whatever weight it last had once VisemeMath::weightsAt() stops
    // reporting it (e.g. after its 30ms decay window fully elapses).
    for (const QString &key : m_lastAppliedShapeKeys) {
        if (!desired.contains(key))
            desired.insert(key, 0.0f);
    }
    applyWeights(desired);
    m_lastAppliedShapeKeys = stillActive;
}

void AvatarController::clearVisemeWeights()
{
    QHash<QString, float> desired;
    for (const QString &key : m_lastAppliedShapeKeys)
        desired.insert(key, 0.0f);
    applyWeights(desired);
    m_lastAppliedShapeKeys.clear();
}

void AvatarController::applyWeights(const QHash<QString, float> &desired)
{
    if (!m_root || desired.isEmpty())
        return;

    // Single tree walk per tick (not one per shape key) -- matches every
    // morph-target-shaped QObject found anywhere under `root` against the
    // desired map by objectName, driving its "weight" property by name
    // (QQuick3DMorphTarget isn't a type this code is allowed to name --
    // see avatar_controller.h).
    QList<QObject *> stack;
    stack.append(m_root);
    while (!stack.isEmpty()) {
        QObject *node = stack.takeLast();
        QQmlListReference ref(node, "morphTargets");
        // Eye and body models carry blink/run targets, not visemes: index 0 there would
        // otherwise be matched to "viseme_A".
        if (ref.isValid() && !isNonVisemeModel(int(ref.count()))) {
            for (qsizetype i = 0; i < ref.count(); ++i) {
                QObject *target = ref.at(i);
                if (!target)
                    continue;
                // Imported morph targets have no objectName; the i-th target
                // corresponds to the i-th manifest shape-key name.
                const QString name = i < m_shapeKeyNames.size() ? m_shapeKeyNames.at(i) : target->objectName();
                const auto it = desired.find(name);
                if (it != desired.end())
                    target->setProperty("weight", it.value());
            }
        }
        stack.append(node->children());
    }
}
