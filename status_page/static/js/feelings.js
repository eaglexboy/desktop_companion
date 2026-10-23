// Feelings are *derived* from the three mood values; nothing here is stored.
export const FEELING_COLORS = {
  happy: "#3fae6a",
  content: "#7ab86b",
  excited: "#e0a030",
  energetic: "#d9822b",
  nervous: "#9b6fd6",
  anxious: "#7d5bbf",
  sad: "#4f7fd1",
  down: "#6c86b8",
  frustrated: "#d0584f",
  sleepy: "#8a8fa3",
  neutral: "#b9bccb",
};

// mood: { valence, energy, stress }, each 0-100 with 50 neutral.
export function feeling(mood) {
  const v = mood.valence ?? 50,
    e = mood.energy ?? 50,
    s = mood.stress ?? 50;
  if (s >= 65) return v <= 40 ? "anxious" : "nervous";
  if (v >= 65) return e >= 60 ? "excited" : "happy";
  if (v <= 35) return e <= 40 ? "sad" : "frustrated";
  if (e <= 30) return "sleepy";
  if (e >= 70) return "energetic";
  if (v >= 55) return "content";
  if (v <= 45) return "down";
  return "neutral";
}
