#include "window_mask_helper.h"

#include <QQuickWindow>
#include <QRect>
#include <QRegion>
#include <QVariantMap>

WindowMaskHelper::WindowMaskHelper(QObject *parent) : QObject(parent) {}

void WindowMaskHelper::applyMask(QQuickWindow *window, int width, int height, int y)
{
    if (!window)
        return;
    window->setMask(QRegion(0, y, width, height));
}

bool WindowMaskHelper::applyRects(QQuickWindow *window, const QVariantList &rects)
{
    if (!window)
        return false;
    QRegion region;
    for (const QVariant &value : rects)
    {
        const QVariantMap m = value.toMap();
        const QRect rect(m.value("x").toInt(), m.value("y").toInt(), m.value("width").toInt(), m.value("height").toInt());
        if (rect.width() > 0 && rect.height() > 0)
            region += rect;
    }
    if (region.isEmpty())
        return false;
    window->setMask(region);
    return true;
}

QVariantMap WindowMaskHelper::opaqueBounds(const QImage &image, int alphaMin) const
{
    if (image.isNull() || !image.hasAlphaChannel())
        return {};
    const QImage argb = image.convertToFormat(QImage::Format_ARGB32);
    int left = argb.width(), top = argb.height(), right = -1, bottom = -1;
    for (int y = 0; y < argb.height(); ++y)
    {
        const QRgb *row = reinterpret_cast<const QRgb *>(argb.constScanLine(y));
        for (int x = 0; x < argb.width(); ++x)
        {
            if (qAlpha(row[x]) <= alphaMin)
                continue;
            left = qMin(left, x);
            right = qMax(right, x + 1);
            top = qMin(top, y);
            bottom = qMax(bottom, y + 1);
        }
    }
    if (right < 0)
        return {};
    return {{"left", left}, {"top", top}, {"right", right}, {"bottom", bottom}, {"imageWidth", argb.width()}, {"imageHeight", argb.height()}};
}
