// LayerShellQt's QML attached `screen` property cannot be assigned from QML
// on this Qt/LayerShellQt build ("Cannot assign QObject* to QScreen*", even
// when handed a properly typed QScreen), and setting plain QWindow::screen
// does not re-home an existing layer-shell surface to another output. The
// C++ API's Window::setScreen() does, so this is the thin bridge Main.qml
// calls to move the overlay's surface to a named output.
#pragma once

#include <QObject>
#include <QQmlEngine>
#include <QQuickWindow>

class LayerShellScreenHelper : public QObject
{
    Q_OBJECT
    QML_ELEMENT

public:
    explicit LayerShellScreenHelper(QObject *parent = nullptr);

    // Returns false (and changes nothing) if no output has that name.
    Q_INVOKABLE bool moveToScreen(QQuickWindow *window, const QString &screenName);
};
