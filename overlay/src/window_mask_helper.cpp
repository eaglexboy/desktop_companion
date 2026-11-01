#include "window_mask_helper.h"

#include <QQuickWindow>
#include <QRegion>

WindowMaskHelper::WindowMaskHelper(QObject *parent) : QObject(parent) {}

void WindowMaskHelper::applyMask(QQuickWindow *window, int width, int height, int y)
{
    if (!window)
        return;
    window->setMask(QRegion(0, y, width, height));
}
