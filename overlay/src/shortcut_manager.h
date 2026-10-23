// Registers global shortcuts via KF6GlobalAccel's documented QAction-based
// C++ API (KGlobalAccel::setShortcut() + the globalShortcutActiveChanged
// signal), which talks to the org.kde.kglobalaccel D-Bus service under the
// hood. Using the documented API avoids hand-rolling raw QDBusInterface
// calls against org.kde.KGlobalAccel's doRegister/setShortcutKeys methods
// and guessing at their undocumented flag bit values.
#pragma once

#include <QAction>
#include <QObject>
#include <QQmlEngine>
#include <QString>

class ShortcutManager : public QObject
{
    Q_OBJECT
    QML_ELEMENT

public:
    explicit ShortcutManager(QObject *parent = nullptr);

    // keySequence e.g. "Meta+Alt+D" (welcome's config.shortcuts.toggle_interaction).
    Q_INVOKABLE void registerToggleInteraction(const QString &keySequence);
    // mode: "hold" (press -> pushToTalkPressed, release -> pushToTalkReleased)
    // or "toggle" (each press -> pushToTalkToggled); matches
    // config.shortcuts.push_to_talk_mode.
    Q_INVOKABLE void registerPushToTalk(const QString &keySequence, const QString &mode);

signals:
    void toggleInteractionTriggered();
    void pushToTalkPressed();
    void pushToTalkReleased();
    void pushToTalkToggled();

private:
    QAction m_toggleInteractionAction;
    QAction m_pushToTalkAction;
    QString m_pushToTalkMode = QStringLiteral("hold");
};
