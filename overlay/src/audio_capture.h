// Captures microphone audio at 16kHz/mono/Int16 and emits 20ms PCM frames
// as they arrive. Owns its own QAudioSource lifecycle, started and stopped
// by ShortcutManager's push-to-talk signals or the InteractionBar's mic
// button -- this class knows nothing about IPC; qml/Main.qml wires its
// chunkReady signal to IpcClient::sendAudioStreamChunk.
#pragma once

#include <QAudioFormat>
#include <QByteArray>
#include <QObject>
#include <QQmlEngine>
#include <QString>
#include <memory>

class QAudioSource;
class QIODevice;

class AudioCapture : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(bool capturing READ isCapturing NOTIFY capturingChanged)

public:
    explicit AudioCapture(QObject *parent = nullptr);
    ~AudioCapture() override;

    Q_INVOKABLE void start(const QString &streamId);
    Q_INVOKABLE void stop();
    bool isCapturing() const { return m_source != nullptr; }

signals:
    void capturingChanged();
    void chunkReady(const QString &streamId, int seq, const QByteArray &pcm16le);

private slots:
    void onReadyRead();

private:
    QAudioFormat m_format;
    std::unique_ptr<QAudioSource> m_source;
    QIODevice *m_device = nullptr;
    QString m_streamId;
    int m_seq = 0;
    QByteArray m_pending;
};
