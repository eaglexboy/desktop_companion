#include "../src/viseme_math.h"

#include <QTest>

class TestVisemeMath : public QObject
{
    Q_OBJECT

private slots:
    void emptyTimelineYieldsNoWeights();
    void attackRampsUpOverTwentyMs();
    void activeFrameHoldsFullWeight();
    void decayRampsDownOverThirtyMsAfterEnd();
    void fullyElapsedFrameYieldsNoWeight();
    void discontinuityJumpSnapsWithoutGliding();
    void adjacentFramesCrossfade();
};

void TestVisemeMath::emptyTimelineYieldsNoWeights()
{
    const std::vector<VisemeFrame> frames;
    QVERIFY(VisemeMath::weightsAt(frames, 0.5).empty());
}

void TestVisemeMath::attackRampsUpOverTwentyMs()
{
    const std::vector<VisemeFrame> frames = {{0.0, 0.5, "viseme_A"}};

    // At the exact instant a frame starts, its computed weight is exactly
    // 0.0 -- weightsAt() omits exactly-zero entries rather than reporting
    // a spurious "active at weight 0" row (see viseme_math.cpp), which is
    // observably identical to the caller (no entry == implicit 0).
    const auto atStart = VisemeMath::weightsAt(frames, 0.0);
    QVERIFY(atStart.empty());

    const auto atHalfAttack = VisemeMath::weightsAt(frames, VisemeMath::kAttackS / 2.0);
    QCOMPARE(atHalfAttack.size(), size_t(1));
    QVERIFY(qFuzzyCompare(atHalfAttack[0].weight, 0.5f) || qAbs(atHalfAttack[0].weight - 0.5f) < 0.01f);

    const auto atFullAttack = VisemeMath::weightsAt(frames, VisemeMath::kAttackS);
    QCOMPARE(atFullAttack.size(), size_t(1));
    QCOMPARE(atFullAttack[0].weight, 1.0f);
}

void TestVisemeMath::activeFrameHoldsFullWeight()
{
    const std::vector<VisemeFrame> frames = {{0.0, 1.0, "viseme_B"}};
    const auto mid = VisemeMath::weightsAt(frames, 0.5);
    QCOMPARE(mid.size(), size_t(1));
    QCOMPARE(mid[0].shapeKey, QStringLiteral("viseme_B"));
    QCOMPARE(mid[0].weight, 1.0f);
}

void TestVisemeMath::decayRampsDownOverThirtyMsAfterEnd()
{
    const std::vector<VisemeFrame> frames = {{0.0, 0.5, "viseme_C"}};

    const auto justAfterEnd = VisemeMath::weightsAt(frames, 0.5);
    QCOMPARE(justAfterEnd.size(), size_t(1));
    QCOMPARE(justAfterEnd[0].weight, 1.0f);

    const auto halfDecay = VisemeMath::weightsAt(frames, 0.5 + VisemeMath::kDecayS / 2.0);
    QCOMPARE(halfDecay.size(), size_t(1));
    QVERIFY(qAbs(halfDecay[0].weight - 0.5f) < 0.01f);
}

void TestVisemeMath::fullyElapsedFrameYieldsNoWeight()
{
    const std::vector<VisemeFrame> frames = {{0.0, 0.5, "viseme_D"}};
    const auto longAfter = VisemeMath::weightsAt(frames, 0.5 + VisemeMath::kDecayS + 1.0);
    QVERIFY(longAfter.empty());
}

void TestVisemeMath::discontinuityJumpSnapsWithoutGliding()
{
    // A clock discontinuity (buffer under-run, device switch, seek) must
    // snap immediately to the target weight rather than gliding across
    // the gap. Since weightsAt() is a pure function of elapsedS with zero
    // incremental state, jumping the sample point by any amount produces
    // exactly the same answer as evaluating that point directly -- there
    // is no state to have "glided" from.
    const std::vector<VisemeFrame> frames = {{0.0, 0.2, "viseme_A"}, {5.0, 5.3, "viseme_E"}};

    const auto direct = VisemeMath::weightsAt(frames, 5.1);
    const auto afterJump = VisemeMath::weightsAt(frames, 5.1); // simulate re-sampling post-jump
    QCOMPARE(direct.size(), afterJump.size());
    QCOMPARE(direct[0].shapeKey, afterJump[0].shapeKey);
    QCOMPARE(direct[0].weight, afterJump[0].weight);
    QCOMPARE(direct[0].shapeKey, QStringLiteral("viseme_E"));
}

void TestVisemeMath::adjacentFramesCrossfade()
{
    // Back-to-back frames with no gap: the outgoing frame should still be
    // reported as decaying downward even while the next frame is also
    // ramping up (a natural crossfade), i.e. both can be non-zero at once.
    const std::vector<VisemeFrame> frames = {{0.0, 0.1, "viseme_A"}, {0.1, 0.3, "viseme_B"}};
    const auto atBoundary = VisemeMath::weightsAt(frames, 0.105);
    QCOMPARE(atBoundary.size(), size_t(2));
}

QTEST_MAIN(TestVisemeMath)
#include "test_viseme_math.moc"
