// QWindow::setMask() is a plain C++ method, not Q_INVOKABLE or a slot, so
// QML can't call it directly. This is the thin bridge Main.qml calls to
// apply the passive (avatar-only) and interactive (avatar + interaction
// bar) click-through input region masks.
#pragma once

#include <QObject>
#include <QQmlEngine>
#include <QQuickWindow>

class WindowMaskHelper : public QObject
{
    Q_OBJECT
    QML_ELEMENT

public:
    explicit WindowMaskHelper(QObject *parent = nullptr);

    Q_INVOKABLE void applyMask(QQuickWindow *window, int width, int height);
};
