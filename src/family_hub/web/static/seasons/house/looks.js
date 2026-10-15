// A house's own seasonal looks. This engine copy is empty on purpose: the
// repo is public, so it ships only art anyone may redistribute. A private
// deploy overlay replaces this file (and adds the photos, their glass/
// copies and a CREDITS.md beside it) with entries like:
//
//   window.FH_HOUSE_LOOKS = [
//     { id: "christmas-house-cabin-night", season: "christmas",
//       name: "Cabin Night", blurb: "A lit cabin under the moon",
//       credit: "Photo from Pixabay", file: "christmas-house-cabin-night.webp",
//       pos: "center 55%", accent: ["#F2C46B", "#2A1C00", "#866013"],
//       default: true },
//   ];
//
// theme.js reads the list (see mergeHouseLooks there and
// docs/seasonal-looks.md, "House looks").
window.FH_HOUSE_LOOKS = window.FH_HOUSE_LOOKS || [];
