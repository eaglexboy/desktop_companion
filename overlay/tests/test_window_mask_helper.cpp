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

QTEST_MAIN(TestWindowMaskHelper)
#include "test_window_mask_helper.moc"
