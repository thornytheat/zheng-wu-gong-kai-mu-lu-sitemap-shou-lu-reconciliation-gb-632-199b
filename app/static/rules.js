/* 地址规则：切换请求走 JSON API，成功后刷新页面；失败弹回原下拉并提示。 */
(function () {
  const forms = document.querySelectorAll("form[data-switch]");
  forms.forEach((form) => {
    const select = form.querySelector("select[name=profile]");
    const original = select ? select.value : "";
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button");
      if (button) button.disabled = true;
      const body = {
        sites: [form.querySelector("input[name=sites]").value],
        profile: select ? select.value : "",
        expected_versions: {},
      };
      const versionInput = form.querySelector("input[name=expected_versions]");
      const site = form.querySelector("input[name=sites]").value;
      if (versionInput && versionInput.value) {
        body.expected_versions[site] = Number(versionInput.value);
      }
      try {
        const response = await fetch("/api/rules/switch", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.message || data.error || "切换失败");
        }
        window.location.reload();
      } catch (error) {
        if (select) select.value = original;
        window.alert(error.message || "切换失败，已保持旧规则");
      } finally {
        if (button) button.disabled = false;
      }
    });
  });
})();
