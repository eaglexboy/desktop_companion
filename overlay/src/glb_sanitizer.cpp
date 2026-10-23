#include "glb_sanitizer.h"

#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>
#include <QtEndian>

namespace {
constexpr quint32 kGlbMagic = 0x46546C67; // "glTF"
constexpr quint32 kChunkJson = 0x4E4F534A; // "JSON"
constexpr int kHeaderSize = 12;
constexpr int kChunkHeaderSize = 8;

void appendU32(QByteArray &out, quint32 value)
{
    char bytes[4];
    qToLittleEndian(value, bytes);
    out.append(bytes, 4);
}
} // namespace

GlbSanitizer::GlbSanitizer(QObject *parent) : QObject(parent) {}

QByteArray GlbSanitizer::stripAnimations(const QByteArray &glb)
{
    if (glb.size() < kHeaderSize + kChunkHeaderSize)
        return {};
    const auto *data = reinterpret_cast<const uchar *>(glb.constData());
    if (qFromLittleEndian<quint32>(data) != kGlbMagic)
        return {};
    const quint32 version = qFromLittleEndian<quint32>(data + 4);

    const quint32 jsonLength = qFromLittleEndian<quint32>(data + kHeaderSize);
    if (qFromLittleEndian<quint32>(data + kHeaderSize + 4) != kChunkJson)
        return {};
    const qsizetype jsonStart = kHeaderSize + kChunkHeaderSize;
    if (jsonStart + qsizetype(jsonLength) > glb.size())
        return {};

    QJsonParseError parseError;
    QJsonDocument doc = QJsonDocument::fromJson(glb.mid(jsonStart, jsonLength), &parseError);
    if (parseError.error != QJsonParseError::NoError || !doc.isObject())
        return {};
    QJsonObject root = doc.object();
    root.remove(QStringLiteral("animations"));

    QByteArray json = QJsonDocument(root).toJson(QJsonDocument::Compact);
    while (json.size() % 4 != 0)
        json.append(' '); // the JSON chunk must be 4-byte aligned, space padded

    const QByteArray rest = glb.mid(jsonStart + jsonLength); // remaining chunks, untouched
    QByteArray out;
    out.reserve(kHeaderSize + kChunkHeaderSize + json.size() + rest.size());
    appendU32(out, kGlbMagic);
    appendU32(out, version);
    appendU32(out, quint32(kHeaderSize + kChunkHeaderSize + json.size() + rest.size()));
    appendU32(out, quint32(json.size()));
    appendU32(out, kChunkJson);
    out.append(json);
    out.append(rest);
    return out;
}

QUrl GlbSanitizer::sanitizedSource(const QUrl &source)
{
    QString path;
    if (source.isLocalFile())
        path = source.toLocalFile();
    else if (source.scheme() == QLatin1String("qrc"))
        path = QLatin1Char(':') + source.path();
    else
        return source;

    QFile in(path);
    if (!in.open(QIODevice::ReadOnly))
        return source;
    const QByteArray original = in.readAll();
    in.close();

    const QByteArray stripped = stripAnimations(original);
    if (stripped.isEmpty())
        return source; // not a GLB we understand: let RuntimeLoader report it

    // Content-addressed, so an unchanged avatar reuses its copy and a new
    // version never collides with an old one.
    const QString name = QString::fromLatin1(QCryptographicHash::hash(original, QCryptographicHash::Sha256).toHex().left(24)) + QStringLiteral(".glb");
    const QString dirPath = QStandardPaths::writableLocation(QStandardPaths::CacheLocation) + QStringLiteral("/glb");
    QDir().mkpath(dirPath);
    const QString outPath = dirPath + QLatin1Char('/') + name;

    if (QFileInfo(outPath).size() != stripped.size()) {
        QFile out(outPath);
        if (!out.open(QIODevice::WriteOnly | QIODevice::Truncate) || out.write(stripped) != stripped.size())
            return source;
    }
    return QUrl::fromLocalFile(outPath);
}
