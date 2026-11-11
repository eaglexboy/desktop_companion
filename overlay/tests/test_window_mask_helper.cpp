#include "../src/window_mask_helper.h"

#include <QGuiApplication>
#include <QQuickWindow>
#include <QTest>

class TestWindowMaskHelper : public QObject
{
    Q_OBJECT

private slots:
    void nullWindowIsIgnored();
    void appliesPassiveMask();
    void appliesInteractiveMask();
    void rectsAreUnitedIntoOneRegion();
    void rectsWithoutAreaAreLeftOut();
    void noRectLeavesTheMaskAlone();
    void opaqueBoundsHoldsWhatIsDrawn();
    void opaqueBoundsOfNothingIsEmpty();
};

void TestWindowMaskHelper::nullWindowIsIgnored()
{
    WindowMaskHelper helper;
    helper.applyMask(nullptr, 320, 320); // must not crash
}

void TestWindowMaskHelper::appliesPassiveMask()
{
    WindowMaskHelper helper;
    QQuickWindow window;
    helper.applyMask(&window, 320, 320);
    QCOMPARE(window.mask(), QRegion(0, 0, 320, 320));
}

void TestWindowMaskHelper::appliesInteractiveMask()
{
    WindowMaskHelper helper;
    QQuickWindow window;
    helper.applyMask(&window, 320, 376);
    QCOMPARE(window.mask(), QRegion(0, 0, 320, 376));
}

static QVariantMap rect(int x, int y, int width, int height)
{
    return {{"x", x}, {"y", y}, {"width", width}, {"height", height}};
}

void TestWindowMaskHelper::rectsAreUnitedIntoOneRegion()
{
    WindowMaskHelper helper;
    QQuickWindow window;
    QVERIFY(helper.applyRects(&window, {rect(10, 20, 100, 150), rect(0, 200, 240, 40)}));
    QCOMPARE(window.mask(), QRegion(10, 20, 100, 150) + QRegion(0, 200, 240, 40));
    QVERIFY(!window.mask().contains(QPoint(200, 50))); // beside the body: clicks go through
}

void TestWindowMaskHelper::rectsWithoutAreaAreLeftOut()
{
    WindowMaskHelper helper;
    QQuickWindow window;
    QVERIFY(helper.applyRects(&window, {rect(10, 20, 0, 150), rect(5, 5, 50, 60), rect(0, 0, 30, -4)}));
    QCOMPARE(window.mask(), QRegion(5, 5, 50, 60));
}

void TestWindowMaskHelper::noRectLeavesTheMaskAlone()
{
    WindowMaskHelper helper;
    QQuickWindow window;
    helper.applyMask(&window, 320, 320);
    QVERIFY(!helper.applyRects(&window, {}));
    QVERIFY(!helper.applyRects(&window, {rect(0, 0, 0, 0)}));
    QVERIFY(!helper.applyRects(nullptr, {rect(0, 0, 10, 10)}));
    QCOMPARE(window.mask(), QRegion(0, 0, 320, 320)); // an empty region would have taken it off
}

void TestWindowMaskHelper::opaqueBoundsHoldsWhatIsDrawn()
{
    WindowMaskHelper helper;
    QImage image(100, 80, QImage::Format_ARGB32);
    image.fill(Qt::transparent);
    for (int y = 10; y < 70; ++y)
        for (int x = 30; x < 55; ++x)
            image.setPixel(x, y, qRgba(200, 20, 20, 255));
    image.setPixel(90, 5, qRgba(0, 0, 0, 10)); // nearly clear: not part of the body
    const QVariantMap box = helper.opaqueBounds(image);
    QCOMPARE(box.value("left").toInt(), 30);
    QCOMPARE(box.value("right").toInt(), 55);
    QCOMPARE(box.value("top").toInt(), 10);
    QCOMPARE(box.value("bottom").toInt(), 70);
    QCOMPARE(box.value("imageWidth").toInt(), 100);
    QCOMPARE(box.value("imageHeight").toInt(), 80);
}

void TestWindowMaskHelper::opaqueBoundsOfNothingIsEmpty()
{
    WindowMaskHelper helper;
    QImage clear(40, 40, QImage::Format_ARGB32);
    clear.fill(Qt::transparent);
    QVERIFY(helper.opaqueBounds(clear).isEmpty());
    QVERIFY(helper.opaqueBounds(QImage()).isEmpty());
}

QTEST_MAIN(TestWindowMaskHelper)
#include "test_window_mask_helper.moc"
