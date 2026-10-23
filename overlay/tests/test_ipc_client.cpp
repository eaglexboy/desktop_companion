// Exercises IpcClient against a fake brain daemon (a real QWebSocketServer
// standing in for the real server) -- verifies the real wire format on
// both directions: the hello sent on connect, every daemon->client event
// this client parses into a Qt signal, and every client->daemon event it
// serializes.
//
// NOTE on the harness design: messages are captured into plain queues by a
// listener wired up the instant the server accepts a connection, NOT via a
// QSignalSpy constructed inside each test body. On a loopback socket, the
// whole connect -> hello -> receive sequence can complete within a single
// QTest::qWait() time slice during init()'s own connection-polling loop,
// so a spy created later in the test body can miss messages that already
// arrived. Queuing from the moment the connection exists has no such race.
#include "../src/ipc_client.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTest>
#include <QWebSocket>
#include <QWebSocketServer>

namespace {
QByteArray makeEnvelope(const QString &type, const QJsonObject &payload)
{
    QJsonObject obj;
    obj["type"] = type;
    obj["timestamp"] = 1718000000.0;
    obj["payload"] = payload;
    return QJsonDocument(obj).toJson(QJsonDocument::Compact);
}
}

class TestIpcClient : public QObject
{
    Q_OBJECT

private slots:
    void init();
    void cleanup();

    void sendsWellFormedHelloOnConnect();
    void parsesWelcomeIntoSignal();
    void parsesSetPositionEvent();
    void parsesSpeechEvent();
    void parsesConfigUpdateIntoSignal();
    void sendsUserTextInputEnvelope();
    void sendsAudioStreamChunkAsBinaryFollowingJsonHeader();
    void ignoresUnknownEventTypes();

private:
    QString nextTextMessage(int timeoutMs = 2000);
    QByteArray nextBinaryMessage(int timeoutMs = 2000);

    QWebSocketServer *m_server = nullptr;
    QWebSocket *m_serverSocket = nullptr;
    IpcClient *m_client = nullptr;
    QStringList m_receivedText;
    QList<QByteArray> m_receivedBinary;
};

void TestIpcClient::init()
{
    m_receivedText.clear();
    m_receivedBinary.clear();

    m_server = new QWebSocketServer(QStringLiteral("fake-brain"), QWebSocketServer::NonSecureMode, this);
    QVERIFY(m_server->listen(QHostAddress::LocalHost, 0));
    connect(m_server, &QWebSocketServer::newConnection, this, [this]() {
        m_serverSocket = m_server->nextPendingConnection();
        // Wired the instant the connection exists -- see the file header
        // comment for why this can't wait until the test body runs.
        connect(m_serverSocket, &QWebSocket::textMessageReceived, this,
                [this](const QString &msg) { m_receivedText.append(msg); });
        connect(m_serverSocket, &QWebSocket::binaryMessageReceived, this,
                [this](const QByteArray &msg) { m_receivedBinary.append(msg); });
    });

    m_client = new IpcClient(this);
    m_client->connectToBrain(QStringLiteral("127.0.0.1"), m_server->serverPort(), QStringLiteral("test_client"));

    for (int i = 0; i < 150 && !m_serverSocket; ++i)
        QTest::qWait(20);
    QVERIFY(m_serverSocket != nullptr);
}

void TestIpcClient::cleanup()
{
    delete m_client;
    m_client = nullptr;
    delete m_server; // also deletes m_serverSocket (child of m_server's connection list)
    m_server = nullptr;
    m_serverSocket = nullptr;
}

QString TestIpcClient::nextTextMessage(int timeoutMs)
{
    for (int waited = 0; m_receivedText.isEmpty() && waited < timeoutMs; waited += 20)
        QTest::qWait(20);
    if (m_receivedText.isEmpty())
        return QString();
    return m_receivedText.takeFirst();
}

QByteArray TestIpcClient::nextBinaryMessage(int timeoutMs)
{
    for (int waited = 0; m_receivedBinary.isEmpty() && waited < timeoutMs; waited += 20)
        QTest::qWait(20);
    if (m_receivedBinary.isEmpty())
        return QByteArray();
    return m_receivedBinary.takeFirst();
}

void TestIpcClient::sendsWellFormedHelloOnConnect()
{
    const QString message = nextTextMessage();
    QVERIFY(!message.isEmpty());

    const QJsonObject obj = QJsonDocument::fromJson(message.toUtf8()).object();
    QCOMPARE(obj.value("type").toString(), QStringLiteral("hello"));
    const QJsonObject payload = obj.value("payload").toObject();
    QCOMPARE(payload.value("protocol_version").toInt(), 1);
    QCOMPARE(payload.value("client_type").toString(), QStringLiteral("overlay"));
    QCOMPARE(payload.value("client_id").toString(), QStringLiteral("test_client"));
    QVERIFY(payload.value("capabilities").toArray().contains(QJsonValue(QStringLiteral("3d_glb"))));
}

void TestIpcClient::parsesWelcomeIntoSignal()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    QSignalSpy welcomeSpy(m_client, &IpcClient::welcomeReceived);
    QJsonObject manifest;
    manifest["current_version"] = 3;
    QJsonObject shortcuts;
    shortcuts["toggle_interaction"] = "Meta+Alt+D";
    QJsonObject config;
    config["shortcuts"] = shortcuts;
    QJsonObject payload;
    payload["protocol_version"] = 1;
    payload["asset_dir"] = "/tmp/deskmate-current";
    payload["manifest"] = manifest;
    payload["config"] = config;
    m_serverSocket->sendTextMessage(QString::fromUtf8(makeEnvelope("welcome", payload)));

    QVERIFY(welcomeSpy.wait(2000));
    const QVariantMap gotManifest = welcomeSpy.at(0).at(0).toMap();
    const QVariantMap gotConfig = welcomeSpy.at(0).at(1).toMap();
    QCOMPARE(gotManifest.value("current_version").toInt(), 3);
    QCOMPARE(gotConfig.value("shortcuts").toMap().value("toggle_interaction").toString(), QStringLiteral("Meta+Alt+D"));
    QCOMPARE(welcomeSpy.at(0).at(2).toString(), QStringLiteral("/tmp/deskmate-current"));
}

void TestIpcClient::parsesSetPositionEvent()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    QSignalSpy posSpy(m_client, &IpcClient::positionChanged);
    QJsonObject payload;
    payload["x"] = 100;
    payload["y"] = 200;
    payload["screen_name"] = "DP-2";
    payload["duration_ms"] = 400;
    payload["hidden"] = false;
    m_serverSocket->sendTextMessage(QString::fromUtf8(makeEnvelope("set_position", payload)));

    QVERIFY(posSpy.wait(2000));
    QCOMPARE(posSpy.at(0).at(0).toInt(), 100);
    QCOMPARE(posSpy.at(0).at(1).toInt(), 200);
    QCOMPARE(posSpy.at(0).at(2).toString(), QStringLiteral("DP-2"));
    QCOMPARE(posSpy.at(0).at(3).toInt(), 400);
    QCOMPARE(posSpy.at(0).at(4).toBool(), false);
}

void TestIpcClient::parsesSpeechEvent()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    QSignalSpy speechSpy(m_client, &IpcClient::speechReceived);
    QJsonObject payload;
    payload["utterance_id"] = "utt_1";
    payload["wav_path"] = "/tmp/utt_1.wav";
    QJsonArray visemes;
    QJsonObject v;
    v["start_s"] = 0.0;
    v["end_s"] = 0.1;
    v["shape_key"] = "viseme_A";
    visemes.append(v);
    payload["visemes"] = visemes;
    m_serverSocket->sendTextMessage(QString::fromUtf8(makeEnvelope("speech", payload)));

    QVERIFY(speechSpy.wait(2000));
    const QVariantMap got = speechSpy.at(0).at(0).toMap();
    QCOMPARE(got.value("utterance_id").toString(), QStringLiteral("utt_1"));
    QCOMPARE(got.value("visemes").toList().size(), 1);
}

void TestIpcClient::parsesConfigUpdateIntoSignal()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    QSignalSpy configSpy(m_client, &IpcClient::configUpdateReceived);
    QJsonObject voice;
    voice["volume"] = 0.4;
    QJsonObject payload;
    payload["voice"] = voice;
    m_serverSocket->sendTextMessage(QString::fromUtf8(makeEnvelope("config_update", payload)));

    QVERIFY(configSpy.wait(2000));
    const QVariantMap got = configSpy.at(0).at(0).toMap();
    QCOMPARE(got.value("voice").toMap().value("volume").toDouble(), 0.4);
}

void TestIpcClient::sendsUserTextInputEnvelope()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    m_client->sendUserTextInput(QStringLiteral("hello there"));
    const QString message = nextTextMessage();
    QVERIFY(!message.isEmpty());

    const QJsonObject obj = QJsonDocument::fromJson(message.toUtf8()).object();
    QCOMPARE(obj.value("type").toString(), QStringLiteral("user_text_input"));
    QCOMPARE(obj.value("payload").toObject().value("text").toString(), QStringLiteral("hello there"));
    QCOMPARE(obj.value("payload").toObject().value("client_id").toString(), QStringLiteral("test_client"));
}

void TestIpcClient::sendsAudioStreamChunkAsBinaryFollowingJsonHeader()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    const QByteArray pcm(640, '\x7f');
    m_client->sendAudioStreamChunk(QStringLiteral("stream_1"), 3, pcm);

    const QString header = nextTextMessage();
    QVERIFY(!header.isEmpty());
    const QJsonObject obj = QJsonDocument::fromJson(header.toUtf8()).object();
    QCOMPARE(obj.value("type").toString(), QStringLiteral("audio_stream_chunk"));
    QCOMPARE(obj.value("payload").toObject().value("seq").toInt(), 3);

    const QByteArray binary = nextBinaryMessage();
    QCOMPARE(binary, pcm);
}

void TestIpcClient::ignoresUnknownEventTypes()
{
    QVERIFY(!nextTextMessage().isEmpty()); // drain hello

    // A reserved/future event type this client doesn't recognize must be
    // silently ignored, not crash or disconnect the socket.
    m_serverSocket->sendTextMessage(QString::fromUtf8(makeEnvelope("agent_task_progress", QJsonObject())));
    QTest::qWait(200);
    QVERIFY(m_client->isConnected());
}

QTEST_MAIN(TestIpcClient)
#include "test_ipc_client.moc"
