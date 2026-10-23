// Bootstrap only. Layer-shell surface properties (which layer, anchors,
// margins, keyboard interactivity) are set declaratively in QML via the
// `org.kde.layershell` import on the root Window -- see qml/Main.qml.
// LayerShellQt::Shell::useLayerShell() is intentionally not called here:
// it has been unnecessary since Qt 6.5, and calling it just produces dead
// code plus a deprecation warning on current Qt/LayerShellQt versions.
#include <QApplication>
#include <QGuiApplication>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QScreen>
#include <QTimer>

#include "audio_capture.h"
#include "ipc_client.h"
#include "shortcut_manager.h"

int main(int argc, char *argv[])
{
    // QApplication (not QGuiApplication): the system tray icon's menu
    // (Qt.labs.platform) is widget-based and aborts at startup without it.
    QApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("deskmate-overlay"));
    app.setOrganizationName(QStringLiteral("deskmate"));
    // When every display disappears (screen lock / DPMS), the compositor
    // destroys the layer-shell surfaces and Qt would see "last window closed"
    // and quit -- cleanly, so a restart-on-failure policy never revived it and
    // the companion silently vanished. Stay alive; the tray icon's Quit is the
    // only deliberate way out.
    app.setQuitOnLastWindowClosed(false);

    // Loopback-only, non-personal defaults; matches brain/config.py's
    // server.host/server.port. Override for local testing without
    // touching the build.
    const QString ipcHost = qEnvironmentVariable("DESKMATE_IPC_HOST", "127.0.0.1");
    const int ipcPort = qEnvironmentVariable("DESKMATE_IPC_PORT", "8765").toInt();
    const QString clientId = qEnvironmentVariable("DESKMATE_CLIENT_ID", "desktop_primary");

    IpcClient ipc;
    AudioCapture audioCapture;
    ShortcutManager shortcuts;

    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("ipc"), &ipc);
    engine.rootContext()->setContextProperty(QStringLiteral("audioCapture"), &audioCapture);
    engine.rootContext()->setContextProperty(QStringLiteral("shortcutManager"), &shortcuts);
    // qml/Main.qml's currentLookPath resolution needs a real value, which
    // only exists once the daemon's `welcome` handshake hands us its real
    // asset_dir.
    engine.rootContext()->setContextProperty(QStringLiteral("dataDir"), QString());

    QObject::connect(&ipc, &IpcClient::welcomeReceived, &engine,
                      [&engine](const QVariantMap &, const QVariantMap &, const QString &assetDir) {
                          engine.rootContext()->setContextProperty(QStringLiteral("dataDir"), assetDir);
                      });

    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed, &app,
        []() { QCoreApplication::exit(-1); }, Qt::QueuedConnection);

    engine.loadFromModule("Deskmate", "Main");

    // Outputs coming back (unlock / wake): ask the QML side to re-show and
    // re-place the avatar. Debounced -- outputs appear one at a time.
    QTimer displayRecovery;
    displayRecovery.setSingleShot(true);
    displayRecovery.setInterval(600);
    QObject::connect(&displayRecovery, &QTimer::timeout, &app, [&engine]() {
        if (!engine.rootObjects().isEmpty())
            QMetaObject::invokeMethod(engine.rootObjects().first(), "recoverDisplay");
    });
    QObject::connect(&app, &QGuiApplication::screenAdded, &displayRecovery, qOverload<>(&QTimer::start));

    ipc.connectToBrain(ipcHost, ipcPort, clientId);

    return app.exec();
}
