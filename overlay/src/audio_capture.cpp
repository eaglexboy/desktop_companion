#include "audio_capture.h"

#include <QAudioDevice>
#include <QAudioSource>
#include <QIODevice>
#include <QMediaDevices>

namespace {
constexpr int kSampleRate = 16000;
// 20ms @ 16kHz/mono/16-bit = 320 samples * 2 bytes/sample.
constexpr int kBytesPerFrame = 640;
}

AudioCapture::AudioCapture(QObject *parent) : QObject(parent)
{
    m_format.setSampleRate(kSampleRate);
    m_format.setChannelCount(1);
    m_format.setSampleFormat(QAudioFormat::Int16);
}

AudioCapture::~AudioCapture() = default;

void AudioCapture::start(const QString &streamId)
{
    if (m_source)
        stop();

    m_streamId = streamId;
    m_seq = 0;
    m_pending.clear();

    const QAudioDevice device = QMediaDevices::defaultAudioInput();
    m_source = std::make_unique<QAudioSource>(device, m_format, this);
    m_device = m_source->start();
    if (m_device)
        connect(m_device, &QIODevice::readyRead, this, &AudioCapture::onReadyRead);
    emit capturingChanged();
}

void AudioCapture::stop()
{
    if (!m_source)
        return;
    m_source->stop();
    m_source.reset();
    m_device = nullptr;
    m_pending.clear();
    emit capturingChanged();
}

void AudioCapture::onReadyRead()
{
    if (!m_device)
        return;
    m_pending.append(m_device->readAll());
    while (m_pending.size() >= kBytesPerFrame) {
        emit chunkReady(m_streamId, m_seq++, m_pending.left(kBytesPerFrame));
        m_pending.remove(0, kBytesPerFrame);
    }
}
