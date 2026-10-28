#include "ipc_client.h"

#include <QDateTime>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QUrl>

namespace {
constexpr int kReconnectIntervalMs = 2000;
constexpr int kProtocolVersion = 1; // must match the brain daemon's protocol version
}

IpcClient::IpcClient(QObject *parent) : QObject(parent)
{
    connect(&m_socket, &QWebSocket::connected, this, &IpcClient::onConnected);
    connect(&m_socket, &QWebSocket::disconnected, this, &IpcClient::onDisconnected);
    connect(&m_socket, &QWebSocket::textMessageReceived, this, &IpcClient::onTextMessageReceived);
    connect(&m_socket, &QWebSocket::errorOccurred, this, [this](QAbstractSocket::SocketError) {
        // swallow -- onDisconnected fires next and schedules a retry
    });

    m_reconnectTimer.setSingleShot(true);
    connect(&m_reconnectTimer, &QTimer::timeout, this, &IpcClient::tryReconnect);
}

void IpcClient::connectToBrain(const QString &host, int port, const QString &clientId)
{
    m_host = host;
    m_port = port;
    m_clientId = clientId;
    tryReconnect();
}

void IpcClient::tryReconnect()
{
    QUrl url;
    url.setScheme(QStringLiteral("ws"));
    url.setHost(m_host);
    url.setPort(m_port);
    url.setPath(QStringLiteral("/ws/overlay"));
    m_socket.open(url);
}

void IpcClient::onConnected()
{
    sendHello();
    m_connected = true;
    emit connectedChanged();
}

void IpcClient::onDisconnected()
{
    m_connected = false;
    emit connectedChanged();
    m_reconnectTimer.start(kReconnectIntervalMs);
}

void IpcClient::sendEnvelope(const QString &type, const QJsonObject &payload)
{
    QJsonObject envelope;
    envelope[QStringLiteral("type")] = type;
    envelope[QStringLiteral("timestamp")] = QDateTime::currentMSecsSinceEpoch() / 1000.0;
    envelope[QStringLiteral("payload")] = payload;
    m_socket.sendTextMessage(QString::fromUtf8(QJsonDocument(envelope).toJson(QJsonDocument::Compact)));
}

void IpcClient::sendHello()
{
    // protocol_version, client_type, client_id, and the capability list
    // the daemon uses to tailor what it sends this client.
    QJsonObject payload;
    payload[QStringLiteral("protocol_version")] = kProtocolVersion;
    payload[QStringLiteral("client_type")] = QStringLiteral("overlay");
    payload[QStringLiteral("client_id")] = m_clientId;
    QJsonArray capabilities;
    for (const auto &cap : {"audio_in", "audio_out", "3d_glb", "shared_filesystem", "desktop_presence"})
        capabilities.append(QString::fromLatin1(cap));
    payload[QStringLiteral("capabilities")] = capabilities;
    sendEnvelope(QStringLiteral("hello"), payload);
}

void IpcClient::onTextMessageReceived(const QString &message)
{
    const QJsonDocument doc = QJsonDocument::fromJson(message.toUtf8());
    if (!doc.isObject())
        return;
    const QJsonObject obj = doc.object();
    const QString type = obj.value(QStringLiteral("type")).toString();
    const QVariantMap payload = obj.value(QStringLiteral("payload")).toObject().toVariantMap();
    handleEnvelope(type, payload);
}

void IpcClient::handleEnvelope(const QString &type, const QVariantMap &payload)
{
    // Anything this client doesn't recognize is ignored rather than
    // treated as an error -- the protocol is meant to grow (reserved
    // future events) without breaking older clients.
    if (type == QStringLiteral("welcome")) {
        emit welcomeReceived(
            payload.value(QStringLiteral("manifest")).toMap(),
            payload.value(QStringLiteral("config")).toMap(),
            payload.value(QStringLiteral("asset_dir")).toString());
    } else if (type == QStringLiteral("chat_response")) {
        emit chatResponseReceived(payload);
    } else if (type == QStringLiteral("set_position")) {
        emit positionChanged(
            payload.value(QStringLiteral("x")).toInt(),
            payload.value(QStringLiteral("y")).toInt(),
            payload.value(QStringLiteral("screen_name")).toString(),
            payload.value(QStringLiteral("duration_ms")).toInt(),
            payload.value(QStringLiteral("hidden")).toBool());
    } else if (type == QStringLiteral("speech")) {
        emit speechReceived(payload);
    } else if (type == QStringLiteral("hot_reload_asset")) {
        emit hotReloadAsset(payload.value(QStringLiteral("manifest")).toMap(), payload.value(QStringLiteral("glb_url")).toString());
    } else if (type == QStringLiteral("status_update")) {
        emit statusUpdateReceived(payload);
    } else if (type == QStringLiteral("appearance_progress")) {
        emit appearanceProgressReceived(payload);
    } else if (type == QStringLiteral("mood_update")) {
        emit moodUpdateReceived(payload);
    } else if (type == QStringLiteral("play_gesture")) {
        emit gestureReceived(payload);
    } else if (type == QStringLiteral("config_update")) {
        emit configUpdateReceived(payload);
    }
}

void IpcClient::sendUserTextInput(const QString &text)
{
    QJsonObject payload;
    payload[QStringLiteral("text")] = text;
    payload[QStringLiteral("client_id")] = m_clientId;
    sendEnvelope(QStringLiteral("user_text_input"), payload);
}

void IpcClient::sendInteractionState(const QString &state)
{
    QJsonObject payload;
    payload[QStringLiteral("state")] = state;
    payload[QStringLiteral("client_id")] = m_clientId;
    sendEnvelope(QStringLiteral("interaction_state"), payload);
}

void IpcClient::sendAssetSwapResult(bool ok, int version, const QString &error)
{
    QJsonObject payload;
    payload[QStringLiteral("ok")] = ok;
    payload[QStringLiteral("version")] = version;
    payload[QStringLiteral("error")] = error.isEmpty() ? QJsonValue(QJsonValue::Null) : QJsonValue(error);
    sendEnvelope(QStringLiteral("asset_swap_result"), payload);
}

void IpcClient::sendSpeechFinished(const QString &utteranceId)
{
    QJsonObject payload;
    payload[QStringLiteral("utterance_id")] = utteranceId;
    sendEnvelope(QStringLiteral("speech_finished"), payload);
}

void IpcClient::sendAudioStreamStart(const QString &streamId)
{
    QJsonObject payload;
    payload[QStringLiteral("stream_id")] = streamId;
    payload[QStringLiteral("sample_rate")] = 16000;
    payload[QStringLiteral("encoding")] = QStringLiteral("pcm16le");
    payload[QStringLiteral("channels")] = 1;
    sendEnvelope(QStringLiteral("audio_stream_start"), payload);
}

void IpcClient::sendAudioStreamChunk(const QString &streamId, int seq, const QByteArray &pcm16le)
{
    // The JSON header is purely sequencing metadata -- the real bytes
    // travel as the very next binary WS frame, routed server-side by
    // whichever stream_id was most recently announced.
    QJsonObject payload;
    payload[QStringLiteral("stream_id")] = streamId;
    payload[QStringLiteral("seq")] = seq;
    sendEnvelope(QStringLiteral("audio_stream_chunk"), payload);
    m_socket.sendBinaryMessage(pcm16le);
}

void IpcClient::sendAudioStreamEnd(const QString &streamId)
{
    QJsonObject payload;
    payload[QStringLiteral("stream_id")] = streamId;
    sendEnvelope(QStringLiteral("audio_stream_end"), payload);
}
