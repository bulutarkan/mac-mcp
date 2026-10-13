// Copy buttons for the install commands and the package hash. The page works without this file.

export function copyText(button, readTarget, writeClipboard) {
  const text = readTarget(button.dataset.copy);
  if (!text) return Promise.resolve(false);
  return writeClipboard(text).then(
    () => {
      button.textContent = "Copied";
      return true;
    },
    () => {
      button.textContent = "Select it";
      return false;
    },
  );
}

if (typeof document !== "undefined") {
  for (const button of document.querySelectorAll("button.copy")) {
    button.hidden = false;
    button.addEventListener("click", () => {
      copyText(
        button,
        (id) => document.getElementById(id)?.textContent.trim(),
        (text) => navigator.clipboard.writeText(text),
      ).then(() => setTimeout(() => (button.textContent = "Copy"), 1600));
    });
  }
}
