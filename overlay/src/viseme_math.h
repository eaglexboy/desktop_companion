// Pure, Qt-Core-only viseme weight math. Deliberately has no dependency on
// Quick3D/rendering so it's unit-testable without a GUI compile --
// avatar_controller.cpp is the only caller, applying the weights this
// returns to the loaded model's actual morph targets.
#pragma once

#include <QString>
#include <vector>

struct VisemeFrame {
    double startS = 0.0;
    double endS = 0.0;
    QString shapeKey;
};

struct VisemeWeight {
    QString shapeKey;
    float weight = 0.0f;
};

namespace VisemeMath {

constexpr double kAttackS = 0.020; // 20ms attack to 1.0
constexpr double kDecayS = 0.030;  // 30ms decay to 0.0

// Direct functional lookup W(t_elapsed): given the full viseme timeline for
// the utterance and an elapsed-time sample, returns every shape key with a
// non-zero weight at that instant (normally 0-2 entries: the active frame
// ramping up via kAttackS, and the just-ended previous frame ramping down
// via kDecayS). Re-evaluated fresh from elapsedS every call with zero
// incremental state, so it is frame-rate independent by construction (a
// 15fps caller and a 60fps caller evaluating the same elapsedS get the same
// answer) and discontinuity-safe for free: a clock jump of any size
// (buffer under-run, device switch, seek) just evaluates a different point
// on the same static timeline next call -- there is nothing to glide
// across, so it "snaps" without needing any special-cased threshold check.
std::vector<VisemeWeight> weightsAt(const std::vector<VisemeFrame> &frames, double elapsedS);

} // namespace VisemeMath
