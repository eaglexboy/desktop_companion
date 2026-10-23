#include "shortcut_manager.h"

#include <KGlobalAccel>
#include <QKeySequence>

ShortcutManager::ShortcutManager(QObject *parent) : QObject(parent)
{
    m_toggleInteractionAction.setParent(this);
    m_pushToTalkAction.setParent(this);

    // Connected exactly once here, not in register*(): the daemon sends
    // `welcome` on every (re)connect, so registering again after a brain
    // restart would stack another handler and fire each keypress twice
    // (toggle_interaction then opened and immediately closed the dialog).
    connect(KGlobalAccel::self(), &KGlobalAccel::globalShortcutActiveChanged, this,
            [this](QAction *action, bool active) {
                if (action == &m_toggleInteractionAction) {
                    if (active)
                        emit toggleInteractionTriggered();
                } else if (action == &m_pushToTalkAction) {
                    if (m_pushToTalkMode == QStringLiteral("toggle")) {
                        if (active)
                            emit pushToTalkToggled();
                    } else if (active) {
                        emit pushToTalkPressed();
                    } else {
                        emit pushToTalkReleased();
                    }
                }
            });
}

void ShortcutManager::registerToggleInteraction(const QString &keySequence)
{
    // objectName is the per-component unique action id KGlobalAccel keys
    // registrations by (KGlobalAccel::setShortcut's own docs: "must have a
    // per main component unique action->objectName()") -- must be set
    // before setShortcut() is called.
    m_toggleInteractionAction.setObjectName(QStringLiteral("toggle_interaction"));
    m_toggleInteractionAction.setText(QStringLiteral("Deskmate: Toggle Interaction"));
    const QKeySequence seq(keySequence);
    KGlobalAccel::self()->setShortcut(&m_toggleInteractionAction, {seq}, KGlobalAccel::NoAutoloading);
}

void ShortcutManager::registerPushToTalk(const QString &keySequence, const QString &mode)
{
    m_pushToTalkMode = mode;
    m_pushToTalkAction.setObjectName(QStringLiteral("push_to_talk"));
    m_pushToTalkAction.setText(QStringLiteral("Deskmate: Push to Talk"));
    const QKeySequence seq(keySequence);
    KGlobalAccel::self()->setShortcut(&m_pushToTalkAction, {seq}, KGlobalAccel::NoAutoloading);
}
