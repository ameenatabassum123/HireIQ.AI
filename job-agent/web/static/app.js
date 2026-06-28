/** Progressive enhancement for filters and async actions. */

(function () {
  "use strict";

  function escapeHtml(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function initSliders() {
    document.querySelectorAll('input[type="range"][data-suffix]').forEach(function (slider) {
      var suffix = slider.getAttribute("data-suffix") || "";
      var valueEl = slider.id ? document.getElementById(slider.id + "-value") : null;
      if (!valueEl && slider.id === "salary-slider") {
        valueEl = document.getElementById("salary-value");
      }
      if (!valueEl && slider.id === "salary-filter") {
        valueEl = document.getElementById("salary-filter-value");
      }
      function update() {
        if (valueEl) valueEl.textContent = "$" + slider.value + suffix;
      }
      slider.addEventListener("input", update);
      update();
    });
  }

  // Mobile nav toggle
  var toggle = document.getElementById("nav-toggle");
  var nav = document.getElementById("main-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      nav.classList.toggle("open");
    });
  }

  // Focus area pills (dashboard)
  var focusPills = document.getElementById("focus-pills");
  if (focusPills) {
    focusPills.addEventListener("click", function (e) {
      var pill = e.target.closest("[data-focus]");
      if (!pill) return;
      focusPills.querySelectorAll(".focus-pill").forEach(function (p) {
        p.classList.remove("active");
      });
      pill.classList.add("active");
    });
  }

  // Upload zone drag-and-drop
  var uploadZone = document.getElementById("upload-zone");
  var fileInput = document.getElementById("resume");
  var fileNameEl = document.getElementById("file-name");
  if (uploadZone && fileInput) {
    ["dragenter", "dragover"].forEach(function (evt) {
      uploadZone.addEventListener(evt, function (e) {
        e.preventDefault();
        uploadZone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      uploadZone.addEventListener(evt, function (e) {
        e.preventDefault();
        uploadZone.classList.remove("dragover");
      });
    });
    uploadZone.addEventListener("drop", function (e) {
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        if (fileNameEl) fileNameEl.textContent = e.dataTransfer.files[0].name;
      }
    });
    fileInput.addEventListener("change", function () {
      if (fileNameEl && fileInput.files[0]) {
        fileNameEl.textContent = fileInput.files[0].name;
      }
    });
  }

  // Jobs list: live filter via fetch with card layout
  var jobsForm = document.getElementById("jobs-filter-form");
  var jobsList = document.getElementById("jobs-list");
  var jobsPageInput = document.getElementById("jobs-page");
  var jobsCountLabel = document.getElementById("jobs-count-label");
  var jobsPagination = document.getElementById("jobs-pagination");
  var loadMoreBtn = document.getElementById("load-more-btn");
  var minScoreHidden = document.getElementById("min-score-hidden");
  var clearFiltersBtn = document.getElementById("clear-filters");

  function syncMinScoreFromCheckboxes() {
    if (!minScoreHidden) return;
    var checked = jobsForm.querySelectorAll("[data-min-score]:checked");
    if (!checked.length) {
      minScoreHidden.value = "";
      jobsForm.querySelectorAll("[data-min-score]").forEach(function (cb) {
        cb.removeAttribute("name");
      });
      return;
    }
    var val = 0;
    checked.forEach(function (cb) {
      val = Math.max(val, parseInt(cb.value, 10));
      cb.removeAttribute("name");
    });
    minScoreHidden.value = String(val);
  }

  if (jobsForm) {
    jobsForm.querySelectorAll("[data-min-score]").forEach(function (cb) {
      cb.addEventListener("change", function () {
        if (cb.checked) {
          jobsForm.querySelectorAll("[data-min-score]").forEach(function (other) {
            if (other !== cb) other.checked = false;
          });
        }
        syncMinScoreFromCheckboxes();
      });
    });
    syncMinScoreFromCheckboxes();
  }

  if (clearFiltersBtn && jobsForm) {
    clearFiltersBtn.addEventListener("click", function () {
      jobsForm.reset();
      if (minScoreHidden) minScoreHidden.value = "";
      jobsForm.querySelectorAll("[data-min-score]").forEach(function (cb) {
        cb.checked = false;
        cb.removeAttribute("name");
      });
      if (jobsPageInput) jobsPageInput.value = "1";
      currentPage = 1;
      loadJobs(true);
    });
  }

  if (jobsForm && jobsList) {
    var debounceTimer;
    var currentPage = jobsPageInput ? parseInt(jobsPageInput.value, 10) || 1 : 1;
    var appendMode = false;
    var jobsHasProfile = jobsList.getAttribute("data-has-profile") === "1";

    function matchRing(score, hasProfile) {
      var n = score == null || score === "" ? null : Number(score);
      var cls = n != null && n < 90 ? "match-ring mid" : "match-ring";
      var pct;
      if (!hasProfile) {
        pct = "—";
        cls = "match-ring mid";
      } else if (n != null) {
        pct = n + "%";
      } else {
        pct = "0%";
      }
      var title = !hasProfile ? ' title="Upload a resume to see match scores"' : "";
      return '<div class="' + cls + '"' + title + '><span class="pct">' + pct + '</span><span class="lbl">Match</span></div>';
    }

    function jobCard(j) {
      var hasProfile = jobsHasProfile;
      var score = j.match_score;
      var applyLabel = score != null && Number(score) >= 90 ? "⚡ Instant Apply" : "Apply Now";
      var desc = j.description ? escapeHtml(j.description.slice(0, 200)) + (j.description.length > 200 ? "…" : "") : "";
      var newBadge = j.is_new ? '<span class="badge badge-new">New</span>' : "";
      return (
        '<article class="job-card" data-job-id="' + j.id + '">' +
        '<div class="company-logo">' + escapeHtml((j.company || j.title || "?").charAt(0).toUpperCase()) + "</div>" +
        '<div class="job-card-body">' +
        '<h2 class="job-card-title"><a href="/jobs/' + j.id + '">' + escapeHtml(j.title || "") + "</a>" + newBadge + "</h2>" +
        '<div class="job-card-meta">' + escapeHtml(j.company || "Unknown") + " · " + escapeHtml(j.location || "Remote") + "</div>" +
        (desc ? '<p class="job-card-desc">' + desc + "</p>" : "") +
        '<div class="keyword-list">' +
        (j.platform ? '<span class="pill pill-gray">' + escapeHtml(j.platform) + "</span>" : "") +
        (j.job_type ? '<span class="pill pill-gray">' + escapeHtml(j.job_type) + "</span>" : "") +
        "</div></div>" +
        matchRing(score, hasProfile) +
        '<div class="job-card-footer">' +
        '<div class="keyword-list"><span class="pill pill-gray">' + escapeHtml((j.date_scraped || "").slice(0, 10)) + "</span></div>" +
        '<div class="job-card-actions">' +
        '<button type="button" class="save-link" data-save-job="' + j.id + '">Save for later</button>' +
        '<a href="/jobs/' + j.id + '" class="btn btn-sm">' + applyLabel + "</a>" +
        "</div></div></article>"
      );
    }

    function renderCards(jobs, replace) {
      if (!jobs.length) {
        var searchVal = (jobsForm.querySelector("#search") || {}).value || "";
        var hint =
          "No jobs match your filters. Try broader terms like <em>data analyst</em> or <em>data scientist</em>, " +
          "or click <strong>Refresh jobs</strong> on the dashboard to scrape new listings.";
        if (searchVal.trim()) {
          hint =
            'No jobs match "<strong>' +
            escapeHtml(searchVal.trim()) +
            "</strong>\". Try broader terms like <em>data analyst</em> or <em>analytics</em>, " +
            "or run a scrape to pull roles from your config.";
        }
        jobsList.innerHTML =
          '<div class="card text-center"><p class="muted-small">' + hint + "</p></div>";
        return;
      }
      var html = jobs.map(jobCard).join("");
      if (replace) {
        jobsList.innerHTML = html;
      } else {
        jobsList.insertAdjacentHTML("beforeend", html);
      }
    }

    function renderPagination(data) {
      if (!jobsPagination) return;
      var page = data.page || 1;
      var totalPages = data.total_pages || 1;
      var html = "";
      if (page < totalPages) {
        html +=
          '<button type="button" class="btn btn-secondary" id="load-more-btn" data-next-page="' +
          (page + 1) +
          '">Load More Recommendations ↓</button>';
      }
      html += '<span class="pagination-info">Page ' + page + " / " + totalPages + "</span>";
      jobsPagination.innerHTML = html;
      var btn = document.getElementById("load-more-btn");
      if (btn) {
        btn.addEventListener("click", function () {
          currentPage = parseInt(btn.getAttribute("data-next-page"), 10);
          if (jobsPageInput) jobsPageInput.value = String(currentPage);
          appendMode = true;
          loadJobs(false);
        });
      }
    }

    function loadJobs(replace) {
      if (replace !== false) appendMode = false;
      syncMinScoreFromCheckboxes();
      var params = new URLSearchParams(new FormData(jobsForm));
      if (!params.get("min_score")) params.delete("min_score");
      params.set("page", String(currentPage));
      if (appendMode && !replace) {
        /* keep loading next page */
      } else if (!appendMode) {
        jobsList.innerHTML = '<div class="card text-center loading">Loading…</div>';
      }
      fetch("/api/jobs?" + params.toString())
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP error " + r.status);
          return r.json();
        })
        .then(function (data) {
          if (typeof data.has_profile === "boolean") {
            jobsHasProfile = data.has_profile;
          }
          renderCards(data.jobs || [], !appendMode || replace);
          if (jobsCountLabel) {
            jobsCountLabel.textContent = (data.total || 0) + " job(s)";
          }
          if (data.scrape && window.updateJobsScrapeStatus) {
            window.updateJobsScrapeStatus(data.scrape);
          }
          renderPagination(data);
          appendMode = false;
        })
        .catch(function () {
          jobsList.innerHTML =
            '<div class="alert alert-error">Failed to load jobs.</div>';
        });
    }

    jobsForm.addEventListener("input", function (e) {
      if (e.target.id === "salary-filter") return;
      currentPage = 1;
      if (jobsPageInput) jobsPageInput.value = "1";
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { loadJobs(true); }, 350);
    });
    jobsForm.addEventListener("change", function () {
      currentPage = 1;
      if (jobsPageInput) jobsPageInput.value = "1";
      loadJobs(true);
    });
    jobsForm.addEventListener("submit", function (e) {
      e.preventDefault();
      loadJobs(true);
    });

    if (loadMoreBtn) {
      loadMoreBtn.addEventListener("click", function () {
        currentPage = parseInt(loadMoreBtn.getAttribute("data-next-page"), 10);
        if (jobsPageInput) jobsPageInput.value = String(currentPage);
        appendMode = true;
        loadJobs(false);
      });
    }

    var jobsScrapeStatus = document.getElementById("jobs-scrape-status");
    var jobsRefreshBtn = document.getElementById("jobs-refresh-btn");
    function formatScrapeStatus(s) {
      if (!s || !s.enabled) return "";
      if (s.is_running) return '<span class="badge badge-scrape-running">Scraping now…</span>';
      var parts = [];
      if (s.minutes_since_last_scrape != null) {
        parts.push("Last updated: " + s.minutes_since_last_scrape + " min ago");
      } else {
        parts.push("Waiting for first scrape…");
      }
      if (s.next_scrape_in_minutes != null) {
        parts.push("Next scrape in " + s.next_scrape_in_minutes + " min");
      }
      return parts.join(" · ");
    }
    window.updateJobsScrapeStatus = function (s) {
      if (jobsScrapeStatus) jobsScrapeStatus.innerHTML = formatScrapeStatus(s);
      if (jobsRefreshBtn) jobsRefreshBtn.disabled = !!(s && s.is_running);
    };
    if (jobsRefreshBtn) {
      jobsRefreshBtn.addEventListener("click", function () {
        jobsRefreshBtn.disabled = true;
        jobsRefreshBtn.textContent = "Refreshing…";
        fetch("/api/scrape/trigger", { method: "POST" })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
          .then(function (res) {
            if (!res.ok) alert(res.data.detail || res.data.error || "Refresh failed.");
            loadJobs(true);
            return fetch("/api/scrape/status").then(function (r) { return r.json(); });
          })
          .then(function (s) { if (s) window.updateJobsScrapeStatus(s); })
          .catch(function () { alert("Refresh request failed."); })
          .finally(function () {
            jobsRefreshBtn.textContent = "Refresh jobs";
            jobsRefreshBtn.disabled = false;
          });
      });
    }
    setInterval(function () {
      fetch("/api/scrape/status")
        .then(function (r) { return r.json(); })
        .then(function (s) { window.updateJobsScrapeStatus(s); })
        .catch(function () { /* ignore */ });
    }, 120000);
  }

  // Save for later (local storage placeholder)
  document.addEventListener("click", function (e) {
    var saveBtn = e.target.closest("[data-save-job]");
    if (!saveBtn) return;
    var id = saveBtn.getAttribute("data-save-job");
    try {
      var saved = JSON.parse(localStorage.getItem("savedJobs") || "[]");
      if (saved.indexOf(id) === -1) saved.push(id);
      localStorage.setItem("savedJobs", JSON.stringify(saved));
      saveBtn.textContent = "Saved ✓";
      saveBtn.disabled = true;
    } catch (err) {
      /* ignore */
    }
  });

  // Profile library: activate / delete / preview / re-parse
  var profileList = document.getElementById("profile-list");
  var previewPanel = document.getElementById("profile-preview");

  function parseStatusBadgeClass(status) {
    return "badge badge-parse-" + (status || "empty");
  }

  function renderProfilePreview(d) {
    if (!previewPanel || !d) return;
    var pdfBlock = "";
    if (d.has_pdf && d.pdf_url) {
      pdfBlock =
        '<div class="pdf-preview-wrap" id="pdf-preview-wrap">' +
        '<object data="' + escapeHtml(d.pdf_url) + '#view=FitH" type="application/pdf" class="pdf-preview-object">' +
        '<iframe src="' + escapeHtml(d.pdf_url) + '#view=FitH" title="Resume PDF preview" class="pdf-preview"></iframe>' +
        "</object>" +
        '<p class="muted-small pdf-fallback-hint">PDF not showing? ' +
        '<a href="' + escapeHtml(d.pdf_url) + '" target="_blank" rel="noopener">Open in a new tab</a>.</p>' +
        "</div>";
    }
    var contact = "";
    if (d.show_contact) {
      var parts = [];
      if (d.profile && d.profile.email) parts.push("✉ " + escapeHtml(d.profile.email));
      if (d.profile && (d.profile.linkedin || d.profile.portfolio)) {
        parts.push("🔗 " + escapeHtml(d.profile.linkedin || d.profile.portfolio));
      }
      if (d.profile && d.profile.location) parts.push("📍 " + escapeHtml(d.profile.location));
      if (parts.length) contact = '<div class="resume-contact">' + parts.join("") + "</div>";
    }
    var note = "";
    if (d.is_local_fallback) {
      note = '<div class="alert alert-info">Basic local extract — full AI parse pending. Click Re-parse with AI when Gemini quota resets.</div>';
    } else if (d.parse_status === "pdf_only") {
      note = '<div class="alert alert-info">PDF uploaded — click Re-parse with AI to extract skills and experience.</div>';
    } else if (d.parse_status === "failed") {
      note = '<div class="alert alert-error">Last parse did not extract usable data. Try Re-parse with AI.</div>';
    }
    var summary = d.summary
      ? '<div class="resume-section-label">Professional Summary</div><p class="preview-summary" style="color:var(--text-secondary);font-size:0.9rem;line-height:1.7">' + escapeHtml(d.summary) + "</p>"
      : "";
    var skills = "";
    if (d.skills && d.skills.length) {
      skills =
        '<div class="resume-section-label">Skills</div><div class="keyword-list">' +
        d.skills.map(function (s) { return '<span class="pill">' + escapeHtml(s) + "</span>"; }).join("") +
        "</div>";
    }
    var actions = d.has_pdf
      ? '<button type="button" class="btn btn-sm btn-secondary btn-reparse" data-slug="' + escapeHtml(d.slug) + '">↻ Re-parse with AI</button>' +
        '<a href="' + escapeHtml(d.pdf_url) + '" class="btn btn-sm btn-secondary" target="_blank" rel="noopener">↓ Open PDF</a>'
      : "";
    previewPanel.innerHTML =
      '<div class="resume-preview-header"><div>' +
      '<h1 style="font-size:1.5rem;margin:0">Resume preview</h1>' +
      '<p class="muted-small" style="color:var(--primary);margin:0.25rem 0 0">Reviewing: ' + escapeHtml(d.slug) + ".pdf</p>" +
      '<span class="' + parseStatusBadgeClass(d.parse_status) + '">' + escapeHtml(d.parse_status_label || "") + "</span>" +
      "</div>" +
      (actions ? '<div class="resume-preview-actions">' + actions + "</div>" : "") +
      "</div>" +
      '<div class="resume-preview-card" id="preview-card">' +
      pdfBlock +
      '<div class="resume-name">' + escapeHtml(d.display_name || d.slug) + "</div>" +
      contact +
      note +
      summary +
      skills +
      "</div>";
  }

  function selectProfileRow(slug) {
    if (!profileList) return;
    profileList.querySelectorAll(".profile-row").forEach(function (row) {
      row.classList.toggle("selected", row.getAttribute("data-slug") === slug);
    });
  }

  function loadProfilePreview(slug, pushUrl) {
    if (!slug) return;
    fetch("/api/profiles/" + encodeURIComponent(slug))
      .then(function (r) {
        if (!r.ok) throw new Error("load failed");
        return r.json();
      })
      .then(function (d) {
        renderProfilePreview(d);
        selectProfileRow(slug);
        if (pushUrl !== false) {
          var url = "/profiles?slug=" + encodeURIComponent(slug);
          if (window.history && window.history.pushState) {
            window.history.pushState({ slug: slug }, "", url);
          }
        }
      })
      .catch(function () {
        window.location.href = "/profiles?slug=" + encodeURIComponent(slug);
      });
  }

  if (profileList) {
    profileList.addEventListener("click", function (e) {
      var link = e.target.closest(".profile-link");
      if (link && !e.target.closest(".btn-activate") && !e.target.closest(".btn-delete")) {
        e.preventDefault();
        var slugLink = link.getAttribute("data-slug");
        if (slugLink) loadProfilePreview(slugLink);
        return;
      }

      var activateBtn = e.target.closest(".btn-activate");
      var deleteBtn = e.target.closest(".btn-delete");
      if (activateBtn) {
        var slugA = activateBtn.getAttribute("data-slug");
        fetch("/api/profiles/" + encodeURIComponent(slugA) + "/activate", { method: "POST" })
          .then(function (r) {
            if (!r.ok) throw new Error("activate failed");
            return r.json();
          })
          .then(function () { loadProfilePreview(slugA); })
          .catch(function () { alert("Failed to set active profile."); });
      }
      if (deleteBtn) {
        var slugD = deleteBtn.getAttribute("data-slug");
        if (!confirm("Delete profile " + slugD + " and its PDF?")) return;
        fetch("/api/profiles/" + encodeURIComponent(slugD), { method: "DELETE" })
          .then(function (r) {
            if (!r.ok) throw new Error("delete failed");
            window.location.href = "/profiles";
          })
          .catch(function () { alert("Failed to delete profile."); });
      }
    });
  }

  if (previewPanel) {
    previewPanel.addEventListener("click", function (e) {
      var reparseBtn = e.target.closest(".btn-reparse");
      if (!reparseBtn) return;
      var slugR = reparseBtn.getAttribute("data-slug");
      if (!slugR) return;
      reparseBtn.disabled = true;
      reparseBtn.textContent = "Parsing…";
      fetch("/api/profiles/" + encodeURIComponent(slugR) + "/parse", { method: "POST" })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          if (!res.ok) {
            alert(res.data.detail || res.data.error || "Parse failed.");
            reparseBtn.disabled = false;
            reparseBtn.textContent = "↻ Re-parse with AI";
            return;
          }
          if (res.data.detail) renderProfilePreview(res.data.detail);
          else loadProfilePreview(slugR, false);
          var banner = document.createElement("div");
          banner.className = "alert alert-success";
          banner.textContent = res.data.message || "Parse complete.";
          previewPanel.insertBefore(banner, previewPanel.firstChild);
        })
        .catch(function () {
          alert("Parse request failed.");
          reparseBtn.disabled = false;
          reparseBtn.textContent = "↻ Re-parse with AI";
        });
    });
  }

  window.addEventListener("popstate", function (ev) {
    var slug = (ev.state && ev.state.slug) || new URLSearchParams(window.location.search).get("slug");
    if (slug) loadProfilePreview(slug, false);
  });

  // ATS calculator: async score + dedicated results panel
  var atsForm = document.getElementById("ats-form");
  var atsOutput = document.getElementById("ats-score-output");
  var atsVisual = document.getElementById("ats-visual-results");
  var atsStatus = document.getElementById("ats-status");
  var atsErrorEl = document.getElementById("ats-error");

  function atsScoreClass(score) {
    var n = Number(score);
    if (n >= 70) return "score-high";
    if (n >= 50) return "score-mid";
    return "score-low";
  }

  function formatAtsReport(data) {
    if (data.report_text) return data.report_text;
    var lines = [
      "ATS Score: " + (data.ats_score != null ? data.ats_score : "—") + "/100",
      "Recommendation: " + (data.recommendation || "—"),
      ""
    ];
    (data.weighted_sections || []).forEach(function (sec) {
      var name = (sec.section || "").replace(/_/g, " ");
      lines.push("  • " + name + ": " + sec.matched + "/" + sec.total + " (" + sec.percent + "%)");
    });
    if (data.matched_keywords && data.matched_keywords.length) {
      lines.push("", "Matched keywords:", "  " + data.matched_keywords.join(", "));
    }
    if (data.missing_keywords && data.missing_keywords.length) {
      lines.push("", "Missing keywords:", "  " + data.missing_keywords.join(", "));
    }
    if (data.tailoring_suggestions && data.tailoring_suggestions.length) {
      lines.push("", "Tailoring suggestions:", "  " + data.tailoring_suggestions.join(", "));
    }
    return lines.join("\n").trim();
  }

  function renderAtsVisual(data, jobTitle) {
    if (!atsVisual) return;
    var score = data.ats_score != null ? Number(data.ats_score) : 0;
    var html =
      '<div class="ats-visual-inner">' +
      (jobTitle ? "<h3>" + escapeHtml(jobTitle) + "</h3>" : "") +
      '<div class="ats-score-ring">' +
      '<div class="ats-score-value ' + atsScoreClass(score) + '">' + escapeHtml(String(score)) + "</div>" +
      '<div><div class="meta-label">ATS compatibility</div>' +
      '<div class="meta-value">' + escapeHtml(data.recommendation || "") + "</div>" +
      '<div class="muted-small">ATS Score: ' + escapeHtml(String(score)) + "/100</div></div></div>";

    if (data.weighted_sections && data.weighted_sections.length) {
      html += "<h3>Score breakdown</h3><div class=\"breakdown-bars\">";
      data.weighted_sections.forEach(function (sec) {
        html +=
          '<div class="breakdown-row">' +
          '<span class="breakdown-label">' + escapeHtml((sec.section || "").replace(/_/g, " ")) + "</span>" +
          '<div class="breakdown-bar"><div class="breakdown-fill" style="width:' + (sec.percent || 0) + '%"></div></div>' +
          '<span class="breakdown-pct">' + sec.matched + "/" + sec.total + "</span></div>";
      });
      html += "</div>";
    }

    html += "<h3>Keywords from job description</h3><div class=\"keyword-list\">";
    (data.matched_keywords || []).forEach(function (kw) {
      html += '<span class="keyword-tag keyword-matched">' + escapeHtml(kw) + "</span>";
    });
    (data.missing_keywords || []).forEach(function (kw) {
      html += '<span class="keyword-tag keyword-missing">' + escapeHtml(kw) + "</span>";
    });
    html += "</div>";

    if (data.tailoring_suggestions && data.tailoring_suggestions.length) {
      html += "<h3>Tailoring suggestions</h3><div class=\"keyword-list\">";
      data.tailoring_suggestions.forEach(function (kw) {
        html += '<span class="keyword-tag keyword-missing">' + escapeHtml(kw) + "</span>";
      });
      html += "</div>";
    }
    html += "</div>";
    atsVisual.innerHTML = html;
  }

  function showAtsError(msg) {
    if (atsErrorEl) {
      atsErrorEl.textContent = msg;
      atsErrorEl.classList.remove("hidden");
    } else if (atsVisual) {
      atsVisual.innerHTML = '<div class="alert alert-error">' + escapeHtml(msg) + "</div>";
    }
  }

  function clearAtsError() {
    if (atsErrorEl) {
      atsErrorEl.textContent = "";
      atsErrorEl.classList.add("hidden");
    }
  }

  if (atsForm && atsForm.getAttribute("data-enhance") === "1") {
    atsForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var jobSelect = atsForm.querySelector('[name="job_id"]');
      var profileSelect = atsForm.querySelector('[name="profile"]');
      var jobId = jobSelect ? jobSelect.value : "";
      if (!jobId) return;
      var jobLabel = jobSelect && jobSelect.selectedIndex >= 0
        ? jobSelect.options[jobSelect.selectedIndex].text
        : "";
      clearAtsError();
      if (atsStatus) atsStatus.textContent = "Calculating ATS score…";
      if (atsOutput) atsOutput.value = "";
      if (atsVisual) atsVisual.innerHTML = "";

      var body = new FormData();
      if (profileSelect && profileSelect.value) body.append("profile_slug", profileSelect.value);

      fetch("/api/ats/" + jobId, { method: "POST", body: body })
        .then(function (r) {
          return r.json().then(function (d) { return { ok: r.ok, data: d }; });
        })
        .then(function (res) {
          if (atsStatus) atsStatus.textContent = "";
          if (!res.ok) {
            var errMsg = res.data.detail || res.data.error || "Could not compute ATS score.";
            showAtsError(errMsg);
            if (atsOutput) atsOutput.value = "Error: " + errMsg;
            return;
          }
          if (atsOutput) atsOutput.value = formatAtsReport(res.data);
          renderAtsVisual(res.data, jobLabel);
          var params = new URLSearchParams();
          params.set("job_id", jobId);
          if (profileSelect && profileSelect.value) params.set("profile", profileSelect.value);
          if (window.history && window.history.replaceState) {
            window.history.replaceState(null, "", "/ats?" + params.toString());
          }
        })
        .catch(function () {
          if (atsStatus) atsStatus.textContent = "";
          showAtsError("Request failed. Check that you have a parsed resume and the job has a description.");
          if (atsOutput) atsOutput.value = "Error: Request failed.";
        });
    });
  }

  // Resume review: generate diffs via API
  var reviewForm = document.getElementById("review-generate-form");
  var reviewResults = document.getElementById("review-results");
  if (reviewForm && reviewResults) {
    reviewForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var jobId = reviewForm.querySelector('[name="job_id"]').value;
      var profileSlug = reviewForm.querySelector('[name="profile_slug"]');
      var statusEl = document.getElementById("review-status");
      var genBtn = document.getElementById("review-generate-btn");
      if (!jobId) return;
      if (statusEl) statusEl.textContent = "Generating tailored resume suggestions (up to 60s)…";
      if (genBtn) genBtn.disabled = true;
      reviewResults.innerHTML = "";
      var body = new FormData();
      if (profileSlug && profileSlug.value) body.append("profile_slug", profileSlug.value);
      var controller = new AbortController();
      var timeoutId = setTimeout(function () { controller.abort(); }, 55000);
      fetch("/api/resume-review/" + jobId + "/propose", {
        method: "POST",
        body: body,
        signal: controller.signal,
      })
        .then(function (r) {
          return r.json().then(function (d) { return { ok: r.ok, data: d }; });
        })
        .then(function (res) {
          clearTimeout(timeoutId);
          if (statusEl) statusEl.textContent = "";
          if (genBtn) genBtn.disabled = false;
          if (!res.ok) {
            var errMsg = res.data.error || res.data.detail || "Request failed.";
            reviewResults.innerHTML = '<div class="alert alert-error">' + escapeHtml(errMsg) + "</div>";
            return;
          }
          if (res.data.error) {
            reviewResults.innerHTML = '<div class="alert alert-error">' + escapeHtml(res.data.error) + "</div>";
            return;
          }
          if (!res.data.diffs || !res.data.diffs.length) {
            reviewResults.innerHTML =
              '<div class="alert alert-info">No changes suggested — your resume already aligns well with this job.</div>';
            return;
          }
          var slugVal = res.data.profile_slug || (profileSlug && profileSlug.value) || "";
          var html = "";
          if (res.data.warning) {
            html += '<div class="alert alert-info">' + escapeHtml(res.data.warning) + "</div>";
          }
          html += '<div class="card"><h2>Suggested changes</h2>' +
            '<form method="post" action="/resume-review/accept">' +
            '<input type="hidden" name="job_id" value="' + escapeHtml(jobId) + '">';
          if (slugVal) {
            html += '<input type="hidden" name="profile_slug" value="' + escapeHtml(slugVal) + '">';
          }
          res.data.diffs.forEach(function (d, i) {
            html +=
              '<div class="diff-item">' +
              '<label><input type="checkbox" name="accepted" value="' + i + '" checked> ' +
              '<span class="diff-section">' + escapeHtml(d.section) + "</span></label>" +
              '<div class="diff-original">' + escapeHtml(d.original_text) + "</div>" +
              '<div class="diff-suggested">' + escapeHtml(d.suggested_text) + "</div>" +
              "</div>";
          });
          html += '<button type="submit" class="btn btn-success">Accept selected &amp; generate PDF</button></form></div>';
          reviewResults.innerHTML = html;
        })
        .catch(function (err) {
          clearTimeout(timeoutId);
          if (statusEl) statusEl.textContent = "";
          if (genBtn) genBtn.disabled = false;
          if (err && err.name === "AbortError") {
            reviewResults.innerHTML =
              '<div class="alert alert-error">Request timed out after 60 seconds. Gemini may be rate-limited — try again shortly or check your API quota.</div>';
            return;
          }
          reviewResults.innerHTML = '<div class="alert alert-error">Request failed. Check that you are logged in and have a parsed resume.</div>';
        });
    });
  }

  // Cover letter: optional async submit
  var coverForm = document.getElementById("cover-letter-form");
  var coverResult = document.getElementById("cover-letter-result");
  if (coverForm && coverResult) {
    coverForm.addEventListener("submit", function (e) {
      if (coverForm.getAttribute("data-nojs") === "1") return;
      e.preventDefault();
      var jobId = coverForm.querySelector('[name="job_id"]').value;
      var statusEl = document.getElementById("cover-letter-status");
      if (!jobId) return;
      if (statusEl) {
        statusEl.classList.remove("hidden");
        statusEl.textContent = "Generating cover letter (may take a minute)…";
      }
      var body = new FormData(coverForm);
      fetch("/api/cover-letter/" + jobId, { method: "POST", body: body })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          if (statusEl) statusEl.classList.add("hidden");
          if (!res.ok) {
            coverResult.innerHTML = '<div class="alert alert-error">' + escapeHtml(res.data.detail || res.data.error || "Failed") + "</div>";
            return;
          }
          coverResult.innerHTML =
            '<div class="card"><h2>Generated letter</h2>' +
            '<div class="description-box">' + escapeHtml(res.data.letter_text || "") + "</div>" +
            (res.data.pdf_path ? '<p style="margin-top:1rem">PDF saved to: <code>' + escapeHtml(res.data.pdf_path) + "</code></p>" : "") +
            "</div>";
        })
        .catch(function () {
          if (statusEl) statusEl.classList.add("hidden");
          coverResult.innerHTML = '<div class="alert alert-error">Request failed.</div>';
        });
    });
  }

  initSliders();
})();
