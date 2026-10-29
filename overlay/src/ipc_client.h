// WebSocket client for the brain daemon's /ws/overlay channel -- see
// brain/ipc/server.py (OverlayHub) for the authoritative message shapes.
// Reconnects with backoff so the overlay stays alive and just keeps
// retrying if the brain daemon isn't up yet or restarts.
#pragma once

#include <QByteArray>
#include <QObject>
#include <QString>
#include <QTimer>
#include <QVariantList>
#include <QVariantMap>
#include <QWebSocket>

class QJsonObject;

class IpcClient : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool connected READ isConnected NOTIFY connectedChanged)

public:
    explicit IpcClient(QObject *parent = nullptr);

    void connectToBrain(const QString &host, int port, const QString &clientId);
    bool isConnected() const { return m_connected; }

    // --- client -> daemon events ---
    Q_INVOKABLE void sendUserTextInput(const QString &text);
    Q_INVOKABLE void sendInteractionState(const QString &state); // "active" | "idle"
    Q_INVOKABLE void sendAssetSwapResult(bool ok, int version, const QString &error);
    Q_INVOKABLE void sendSpeechFinished(const QString &utteranceId);
    Q_INVOKABLE void sendAudioStreamStart(const QString &streamId);
    Q_INVOKABLE void sendAudioStreamChunk(const QString &streamId, int seq, const QByteArray &pcm16le);
    Q_INVOKABLE void sendAudioStreamEnd(const QString &streamId);

signals:
    void connectedChanged();
    // Fired once per connection after the hello/welcome handshake
    // completes. `manifest`/`config` are the welcome payload's own
    // sub-objects; `assetDir` is where glb_filename values from the
    // manifest resolve relative to.
    void welcomeReceived(const QVariantMap &manifest, const QVariantMap &config, const QString &assetDir);
    void chatResponseReceived(const QVariantMap &payload);
    // x, y are top-left offsets within `screenName`'s own output; hidden
    // means "tuck away, unavoidable fullscreen overlap".
    void positionChanged(int x, int y, const QString &screenName, int durationMs, bool hidden);
    void speechReceived(const QVariantMap &payload);
    void hotReloadAsset(const QVariantMap &manifest, const QString &glbUrl);
    void statusUpdateReceived(const QVariantMap &payload);
    // stage: designing | building | done | staged | failed (see brain/evolution.py)
    void appearanceProgressReceived(const QVariantMap &payload);
    void moodUpdateReceived(const QVariantMap &payload);
    void gestureReceived(const QVariantMap &payload);
    void configUpdateReceived(const QVariantMap &payload);

private slots:
    void onConnected();
    void onDisconnected();
    void onTextMessageReceived(const QString &message);
    void tryReconnect();

private:
    void sendEnvelope(const QString &type, const QJsonObject &payload);
    void sendHello();
    void handleEnvelope(const QString &type, const QVariantMap &payload);

    QWebSocket m_socket;
    QString m_host;
    int m_port = 8765;
    QString m_clientId;
    bool m_connected = false;
    QTimer m_reconnectTimer;
};
