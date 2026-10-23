#include "window_mask_helper.h"

#include <QQuickWindow>
#include <QRegion>

WindowMaskHelper::WindowMaskHelper(QObject *parent) : QObject(parent) {}

void WindowMaskHelper::applyMask(QQuickWindow *window, int width, int height)
{
    if (!window)
        return;
    window->setMask(QRegion(0, 0, width, height));
}
