// Copyright (c) 2026 ot2i7ba
// https://github.com/ot2i7ba/
// This code is licensed under the MIT License (see LICENSE for details).

(function () {
  "use strict";

  var G = window.GEOSNAP;
  // Embedded categories missing from the catalogue get an "Other" group, so their layers can still be toggled.
  var otherCategories = Object.keys(G.placeLayers).filter(function (category) { return !G.PLACE_STYLES[category]; }).sort();
  var order = G.PLACE_CATEGORY_ORDER.concat(otherCategories);
  var boxes = {};
  var windowElement = document.getElementById("places-window");
  var selectedSummary = document.getElementById("places-selected");
  var hiddenHint = document.getElementById("places-hidden-hint");
  var groupsContainer = document.getElementById("places-groups");
  var viewBox = document.getElementById("view-places");
  var embeddedTotal = order.reduce(function (sum, category) { return sum + (G.placeCounts[category] || 0); }, 0);

  var element = G.element;

  function button(text, className, onClick) {
    var node = element("button", text, className);
    node.type = "button";
    node.addEventListener("click", onClick);
    return node;
  }

  G.activePlaceCategories = function () {
    return order.filter(function (category) { return boxes[category].checked; });
  };

  // Profiles for "Check surroundings": they preselect categories, the ticks stay
  // freely changeable; a selection matching no profile reads "Custom".
  var PROFILES = [
    { key: "hazards", label: "Hazards", categories: ["water", "railway_line", "bridge", "hazard_site", "forest", "park"] },
    { key: "shelter", label: "Shelter and help", categories: ["police", "hospital", "social_facility", "camp_shelter", "lodging", "place_of_worship"] },
    { key: "travel", label: "Onward travel", categories: ["railway_station", "transport_stop", "fuel_station", "airport_harbour", "parking"] },
    { key: "traces", label: "Recorded traces", categories: ["surveillance_camera", "bank_atm", "supermarket", "fuel_station", "pharmacy"] }
  ].map(function (profile) {
    return { key: profile.key, label: profile.label, categories: profile.categories.filter(function (category) { return G.PLACE_STYLES[category]; }) };
  }).filter(function (profile) { return profile.categories.length > 0; });
  var CUSTOM_PROFILE = "custom";
  G.PLACE_PROFILES = PROFILES;
  var profileSelect = document.getElementById("places-profile");
  PROFILES.concat([{ key: CUSTOM_PROFILE, label: "Custom (ticked categories)" }]).forEach(function (profile) {
    var option = element("option", profile.label);
    option.value = profile.key;
    profileSelect.appendChild(option);
  });
  function matchingProfile() {
    var active = G.activePlaceCategories();
    return PROFILES.filter(function (profile) {
      return profile.categories.length === active.length && profile.categories.every(function (category) { return active.indexOf(category) >= 0; });
    })[0] || null;
  }
  G.placeProfileLabel = function () {
    var profile = matchingProfile();
    return profile ? profile.label : "Custom";
  };
  G.applyPlaceProfile = function (key) {
    var profile = PROFILES.filter(function (candidate) { return candidate.key === key; })[0];
    if (!profile) { return; }
    order.forEach(function (category) { boxes[category].checked = profile.categories.indexOf(category) >= 0; });
    applySelection();
  };
  profileSelect.addEventListener("change", function () {
    if (profileSelect.value !== CUSTOM_PROFILE) { G.applyPlaceProfile(profileSelect.value); }
  });

  function applySelection() {
    var active = G.activePlaceCategories();
    var profile = matchingProfile();
    profileSelect.value = profile ? profile.key : CUSTOM_PROFILE;
    selectedSummary.textContent = active.length + " of " + order.length + " categories";
    hiddenHint.hidden = !(active.length === 0 && embeddedTotal > 0);
    hiddenHint.textContent = hiddenHint.hidden ? "" : embeddedTotal + (embeddedTotal === 1 ? " embedded place is" : " embedded places are") + " hidden: tick categories to show them";
    G.setEmbeddedPlacesVisible(active);
  }

  function setChecked(categories, checked) {
    categories.forEach(function (category) { boxes[category].checked = checked; });
    applySelection();
  }

  function categoryRow(category) {
    var style = G.PLACE_STYLES[category] || { emoji: "?", label: category };
    var row = element("label", undefined, "places-category-row");
    var box = element("input");
    box.type = "checkbox";
    box.checked = false;
    box.dataset.category = category;
    box.addEventListener("change", applySelection);
    boxes[category] = box;
    var count = G.placeCounts[category] || 0;
    row.appendChild(box);
    row.appendChild(element("span", style.emoji, "place-emoji"));
    row.appendChild(element("span", style.label));
    row.appendChild(element("span", count + " embedded", "places-count" + (count > 0 ? " has-embedded" : "")));
    return row;
  }

  // One block per catalogue group: heading with its colour and All/None, then the category rows.
  var groups = otherCategories.length
    ? G.PLACE_GROUPS.concat([{ name: "Other (not in this catalogue)", colour: "#555555", categories: otherCategories }])
    : G.PLACE_GROUPS;
  groups.forEach(function (group) {
    var block = element("div", undefined, "places-group");
    var heading = element("div", undefined, "places-group-title");
    var swatch = element("span", undefined, "place-swatch");
    swatch.style.background = group.colour;
    heading.appendChild(swatch);
    heading.appendChild(element("strong", group.name));
    heading.appendChild(button("All", "places-group-all", function () { setChecked(group.categories, true); }));
    heading.appendChild(button("None", "places-group-none", function () { setChecked(group.categories, false); }));
    block.appendChild(heading);
    group.categories.forEach(function (category) { block.appendChild(categoryRow(category)); });
    groupsContainer.appendChild(block);
  });

  document.getElementById("places-all").addEventListener("click", function () { setChecked(order, true); });
  document.getElementById("places-none").addEventListener("click", function () { setChecked(order, false); });

  var embedded = document.getElementById("places-embedded");
  var present = order.filter(function (category) { return (G.placeCounts[category] || 0) > 0; });
  embedded.textContent = present.length
    ? "Embedded places (see overpass_<stamp>.json): " + present.map(function (category) {
        return (G.PLACE_STYLES[category] ? G.PLACE_STYLES[category].label : category) + " " + G.placeCounts[category];
      }).join(", ")
    : "No embedded places in this project.";

  function applyWindowVisibility() {
    windowElement.hidden = !viewBox.checked;
    if (!windowElement.hidden) { G.setWindowCollapsed(windowElement, false); }
  }
  viewBox.addEventListener("change", applyWindowVisibility);
  document.getElementById("places-close").addEventListener("click", function () {
    viewBox.checked = false;
    applyWindowVisibility();
  });
  applySelection();
  applyWindowVisibility();
})();
