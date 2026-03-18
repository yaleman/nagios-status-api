document.querySelectorAll("details").forEach((element) => {
  element.addEventListener("toggle", () => {
    if (element.open) {
      element.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
});
