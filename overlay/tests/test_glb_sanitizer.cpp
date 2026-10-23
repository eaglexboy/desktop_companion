#include "../src/glb_sanitizer.h"

#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>
#include <QTemporaryDir>
#include <QTest>
#include <QtEndian>

namespace {

void appendU32(QByteArray &out, quint32 v)
{
    char b[4];
    qToLittleEndian(v, b);
    out.append(b, 4);
}

// Minimal valid GLB: a JSON chunk plus one BIN chunk.
QByteArray makeGlb(const QJsonObject &root, const QByteArray &bin)
{
    QByteArray json = QJsonDocument(root).toJson(QJsonDocument::Compact);
    while (json.size() % 4)
        json.append(' ');
    QByteArray binPadded = bin;
    while (binPadded.size() % 4)
        binPadded.append('\0');
    QByteArray out;
    appendU32(out, 0x46546C67);
    appendU32(out, 2);
    appendU32(out, quint32(12 + 8 + json.size() + 8 + binPadded.size()));
    appendU32(out, quint32(json.size()));
    appendU32(out, 0x4E4F534A);
    out.append(json);
    appendU32(out, quint32(binPadded.size()));
    appendU32(out, 0x004E4942);
    out.append(binPadded);
    return out;
}

QJsonObject jsonOf(const QByteArray &glb)
{
    const quint32 len = qFromLittleEndian<quint32>(reinterpret_cast<const uchar *>(glb.constData()) + 12);
    return QJsonDocument::fromJson(glb.mid(20, len)).object();
}

QJsonObject sampleRoot()
{
    return QJsonObject{
        {"asset", QJsonObject{{"version", "2.0"}}},
        {"nodes", QJsonArray{QJsonObject{{"name", "head"}}}},
        {"skins", QJsonArray{QJsonObject{{"name", "Rig"}}}},
        {"animations", QJsonArray{QJsonObject{{"name", "idle_lookaround"}}, QJsonObject{{"name", "think"}}}},
    };
}

} // namespace

class TestGlbSanitizer : public QObject
{
    Q_OBJECT

private slots:
    void removesAnimationsKeepsEverythingElse();
    void preservesBinaryChunkByteForByte();
    void rejectsNonGlb();
    void sanitizedSourceWritesCopyAndPassesThroughBadInput();
};

void TestGlbSanitizer::removesAnimationsKeepsEverythingElse()
{
    const QByteArray stripped = GlbSanitizer::stripAnimations(makeGlb(sampleRoot(), "abcd"));
    QVERIFY(!stripped.isEmpty());
    const QJsonObject root = jsonOf(stripped);
    QVERIFY(!root.contains("animations"));
    QVERIFY(root.contains("skins"));
    QCOMPARE(root["nodes"].toArray().size(), 1);
    // total length field matches the real size
    QCOMPARE(qFromLittleEndian<quint32>(reinterpret_cast<const uchar *>(stripped.constData()) + 8), quint32(stripped.size()));
    QCOMPARE(stripped.size() % 4, 0);
}

void TestGlbSanitizer::preservesBinaryChunkByteForByte()
{
    const QByteArray bin = QByteArray::fromHex("00112233445566778899aabbccddeeff");
    const QByteArray stripped = GlbSanitizer::stripAnimations(makeGlb(sampleRoot(), bin));
    QVERIFY(stripped.endsWith(bin));
}

void TestGlbSanitizer::rejectsNonGlb()
{
    QVERIFY(GlbSanitizer::stripAnimations("").isEmpty());
    QVERIFY(GlbSanitizer::stripAnimations("this is definitely not a glb container").isEmpty());
    QByteArray badJson = makeGlb(sampleRoot(), "abcd");
    badJson[20] = '#'; // replace the opening brace: no longer valid JSON
    QVERIFY(GlbSanitizer::stripAnimations(badJson).isEmpty());
}

void TestGlbSanitizer::sanitizedSourceWritesCopyAndPassesThroughBadInput()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    QStandardPaths::setTestModeEnabled(true);

    const QString good = dir.filePath("good.glb");
    {
        QFile f(good);
        QVERIFY(f.open(QIODevice::WriteOnly));
        f.write(makeGlb(sampleRoot(), "abcd"));
    }
    GlbSanitizer sanitizer;
    const QUrl out = sanitizer.sanitizedSource(QUrl::fromLocalFile(good));
    QVERIFY(out.isLocalFile());
    QVERIFY(out.toLocalFile() != good);
    QFile copy(out.toLocalFile());
    QVERIFY(copy.open(QIODevice::ReadOnly));
    QVERIFY(!jsonOf(copy.readAll()).contains("animations"));
    // same input -> same cached copy
    QCOMPARE(sanitizer.sanitizedSource(QUrl::fromLocalFile(good)), out);

    // missing / corrupt / foreign-scheme sources are returned unchanged
    const QUrl missing = QUrl::fromLocalFile(dir.filePath("nope.glb"));
    QCOMPARE(sanitizer.sanitizedSource(missing), missing);
    const QString corrupt = dir.filePath("corrupt.glb");
    {
        QFile f(corrupt);
        QVERIFY(f.open(QIODevice::WriteOnly));
        f.write("not a glb");
    }
    QCOMPARE(sanitizer.sanitizedSource(QUrl::fromLocalFile(corrupt)), QUrl::fromLocalFile(corrupt));
    const QUrl remote("https://example.com/a.glb");
    QCOMPARE(sanitizer.sanitizedSource(remote), remote);
}

QTEST_MAIN(TestGlbSanitizer)
#include "test_glb_sanitizer.moc"
