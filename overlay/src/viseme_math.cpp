#include "viseme_math.h"

#include <algorithm>

namespace VisemeMath {

std::vector<VisemeWeight> weightsAt(const std::vector<VisemeFrame> &frames, double elapsedS)
{
    std::vector<VisemeWeight> result;
    for (const VisemeFrame &frame : frames) {
        float weight = 0.0f;
        if (elapsedS >= frame.startS && elapsedS < frame.endS) {
            const double sinceStart = elapsedS - frame.startS;
            weight = static_cast<float>(std::min(1.0, sinceStart / kAttackS));
        } else if (elapsedS >= frame.endS && elapsedS < frame.endS + kDecayS) {
            const double sinceEnd = elapsedS - frame.endS;
            weight = static_cast<float>(std::max(0.0, 1.0 - sinceEnd / kDecayS));
        }
        if (weight > 0.0f)
            result.push_back({frame.shapeKey, weight});
    }
    return result;
}

} // namespace VisemeMath
