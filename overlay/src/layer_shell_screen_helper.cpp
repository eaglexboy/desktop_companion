#include "layer_shell_screen_helper.h"

#include <LayerShellQt/Window>
#include <QGuiApplication>
#include <QScreen>

LayerShellScreenHelper::LayerShellScreenHelper(QObject *parent) : QObject(parent) {}

bool LayerShellScreenHelper::moveToScreen(QQuickWindow *window, const QString &screenName)
{
    if (!window)
        return false;
    for (QScreen *screen : QGuiApplication::screens()) {
        if (screen->name() != screenName)
            continue;
        if (auto *layerWindow = LayerShellQt::Window::get(window)) {
            layerWindow->setScreen(screen);
            return true;
        }
        return false;
    }
    return false;
}
