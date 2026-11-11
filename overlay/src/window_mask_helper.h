// QWindow::setMask() is a plain C++ method, not Q_INVOKABLE or a slot, so
// QML can't call it directly. This is the thin bridge Main.qml calls to
// apply the passive (avatar-only) and interactive (avatar + interaction
// bar) click-through input region masks.
#pragma once

#include <QImage>
#include <QObject>
#include <QQmlEngine>
#include <QQuickWindow>
#include <QVariantList>

class WindowMaskHelper : public QObject
{
    Q_OBJECT
    QML_ELEMENT

public:
    explicit WindowMaskHelper(QObject *parent = nullptr);

    Q_INVOKABLE void applyMask(QQuickWindow *window, int width, int height, int y = 0);
    // The input region is the union of `rects` (maps with x, y, width, height, in window
    // coordinates): the box around the body and the interaction bar, so a click anywhere
    // else goes through to what is behind the companion. A rectangle without area is left
    // out. Returns false and leaves the mask as it is when none is left: an empty region
    // would remove the mask, and the whole window would take the clicks.
    Q_INVOKABLE bool applyRects(QQuickWindow *window, const QVariantList &rects);
    // The smallest box that holds every pixel of `image` drawn with an alpha above `alphaMin`, as
    // {left, top, right, bottom, imageWidth, imageHeight} in the image's pixels (right and bottom
    // are the first pixel past the box); an empty map when nothing is drawn. Used once per look, on a
    // picture of the avatar, to learn how far the body reaches beyond its joints.
    Q_INVOKABLE QVariantMap opaqueBounds(const QImage &image, int alphaMin = 24) const;
};
