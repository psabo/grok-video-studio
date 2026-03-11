import React, { useMemo, useRef, useState } from "react";

const API_PORT = import.meta.env.VITE_API_PORT || "8989";
const API_BASE = `http://127.0.0.1:${API_PORT}`;

const defaultSystemInstructions =
  "IMPORTANT: DO NOT INCLUDE BACKGROUND AUDIO OR GENERATE VOICE OVERS DESCRIBING THE SCENE. " +
  "Maintain accurate human anatomy and realistic movements throughout - no extra limbs, distortions, " +
  "morphing, impossible poses, or unnatural behaviors; ensure all elements remain consistent, " +
  "physically plausible, and grounded in reality. Continue seamlessly from the provided image.";

const resolutionOptions = [
  { value: "480p", label: "480p" },
  { value: "720p", label: "720p" },
];

function SectionHeader({ title, subtitle }) {
  return (
    <div className="section-header">
      <h2>{title}</h2>
      <p>{subtitle}</p>
    </div>
  );
}

function LogPanel({ logs }) {
  if (!logs?.length) {
    return <div className="empty-panel">No logs yet.</div>;
  }
  return (
    <div className="log-panel">
      {logs.map((line, index) => (
        <div key={`${line}-${index}`} className="log-line">
          {line}
        </div>
      ))}
    </div>
  );
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      className="ghost-button small-button copy-button"
      onClick={() => {
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }}
      title="Copy prompt"
      style={{ marginLeft: "auto", display: "block", marginTop: "4px" }}
    >
      {copied ? "Copied!" : "Copy Prompt"}
    </button>
  );
}

function OutputList({ sessionId, outputs, onFork }) {
  if (!outputs?.length) {
    return <div className="empty-panel">No outputs yet.</div>;
  }
  return (
    <div className="output-carousel">
      <div className="output-scroll">
        {outputs.map((output, index) => {
          const path = `${API_BASE}/api/sessions/${sessionId}/files/${output.path}`;
          const label = output.type === "preflight" ? "Preflight" : "Video";
          const isAborted = output.status === "aborted";
          return (
            <div
              key={`${output.path}-${index}`}
              className={`output-card ${isAborted ? "aborted" : ""}`}
            >
              <div className="output-meta">
                <span className={`badge ${isAborted ? "badge-aborted" : ""}`}>
                  {isAborted ? "Rejected" : label}
                </span>
                {output.part ? <span className="part">Part {output.part}</span> : null}
                {output.cost ? (
                  <span className="cost-tag">${output.cost.total_cost.toFixed(2)}</span>
                ) : null}
              </div>
              <video controls src={path} />
              {output.prompt && (
                <>
                  <div className="output-prompt">{output.prompt}</div>
                  <CopyButton text={output.prompt} />
                </>
              )}
              <div className="output-actions">
                <a href={path} target="_blank" rel="noreferrer">
                  Open in new tab
                </a>
                {output.type === "video" && !isAborted && onFork && (
                  <button
                    className="ghost-button small-button"
                    onClick={() => onFork(sessionId, output.part)}
                    title="Start new session from this clip"
                  >
                    Fork
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PlaylistLinks({ sessionId, playlists }) {
  if (!playlists?.final && !playlists?.preflight) {
    return null;
  }
  return (
    <div className="playlist-links">
      {playlists.preflight && (
        <a
          href={`${API_BASE}/api/sessions/${sessionId}/files/${playlists.preflight}`}
          target="_blank"
          rel="noreferrer"
        >
          Preflight playlist
        </a>
      )}
      {playlists.final && (
        <a
          href={`${API_BASE}/api/sessions/${sessionId}/files/${playlists.final}`}
          target="_blank"
          rel="noreferrer"
        >
          Final playlist
        </a>
      )}
    </div>
  );
}

const ACTION_LABELS = {
  extend: "Extend (next clip)",
  regenerate: "Regenerate",
  end: "End Session",
  continue: "Continue",
  continue_generation: "Continue to Generate",
  abort: "Abort",
  retry: "Retry",
  stop: "Stop",
  use_refined: "Use Refined",
  use_original: "Use Original",
  accept: "Accept",
};

const PENDING_LABELS = {
  clip_done: "Clip generated successfully",
  retry: "An error occurred",
  retry_preflight: "Preflight failed",
  retry_generation: "Generation failed",
  preflight_review: "Review preflight result",
};

function PendingAction({ sessionId, pendingAction, allowedActions, onAction }) {
  if (!pendingAction || !allowedActions?.length) {
    return null;
  }
  return (
    <div className="pending-action">
      <h4>Action Required</h4>
      <p>{PENDING_LABELS[pendingAction] || pendingAction.replace(/_/g, " ")}</p>
      <div className="action-buttons">
        {allowedActions.map((action) => (
          <button key={action} onClick={() => onAction(sessionId, action)}>
            {ACTION_LABELS[action] || action.replace(/_/g, " ")}
          </button>
        ))}
      </div>
    </div>
  );
}

function StatusStepper({ session }) {
  const { status, current_part, total_parts, pending_action } = session;
  const isPreflight = session.settings?.preflight;
  const steps = [];

  // Determine steps based on mode
  if (isPreflight) {
    const isPfDone = status !== "queued" && (status !== "running" || current_part > 0 || (current_part === 0 && pending_action !== null)); 
    // Simplified logic: 
    // If output has preflight, it's done.
    const hasPreflightOut = session.outputs?.some(o => o.type === "preflight");
    steps.push({ label: "Preflight", status: hasPreflightOut ? "done" : (status === "running" && current_part === 0 ? "active" : "pending") });
  }

  // Generation steps
  // If multi-part, we show "Generating (i/N)" as one active step, or done if finished.
  const isGenerating = status === "running" || status === "waiting";
  const isComplete = status === "completed";
  const isStopped = status === "stopped" || status === "failed";
  
  let genStatus = "pending";
  if (isComplete) genStatus = "done";
  else if (isStopped) genStatus = "error"; // visual indicator for stop/fail
  else if (isGenerating) genStatus = "active";
  
  const genLabel = isGenerating 
    ? `Generating (${current_part}/${total_parts})` 
    : isComplete 
      ? `Generated (${total_parts} clips)` 
      : "Generation";

  steps.push({ label: genLabel, status: genStatus });

  // Review step
  const isReviewing = status === "waiting";
  steps.push({ label: "Review", status: isReviewing ? "active" : (isComplete ? "done" : "pending") });

  // Completion step
  steps.push({ label: "Done", status: isComplete ? "done" : (isStopped ? "error" : "pending") });

  return (
    <div className="status-stepper">
      {steps.map((step, i) => (
        <React.Fragment key={i}>
          <div className={`step ${step.status}`}>
            <div className="step-dot"></div>
            <span className="step-label">{step.label}</span>
          </div>
          {i < steps.length - 1 && <div className={`step-line ${step.status === "done" ? "done" : ""}`}></div>}
        </React.Fragment>
      ))}
    </div>
  );
}

function PromptDiff({ original, refined }) {
  if (!original || !refined) {
    return null;
  }
  return (
    <div className="prompt-diff">
      <div>
        <h4>Original Prompt</h4>
        <pre>{original}</pre>
      </div>
      <div>
        <h4>Refined Prompt</h4>
        <pre>{refined}</pre>
      </div>
    </div>
  );
}

function LatestOutput({ sessionId, outputs }) {
  if (!outputs?.length) {
    return <div className="empty-panel">No outputs yet.</div>;
  }
  const lastVideo =
    [...outputs].reverse().find((output) => output.type === "video") ?? outputs[outputs.length - 1];
  const path = `${API_BASE}/api/sessions/${sessionId}/files/${lastVideo.path}`;
  const label = lastVideo.type === "preflight" ? "Preflight check" : "Final clip";
  return (
    <div className="output-hero">
      <div className="output-meta">
        <span className="badge">{label}</span>
        {lastVideo.part ? <span className="part">Part {lastVideo.part}</span> : null}
        {lastVideo.cost ? (
          <span className="cost-tag">${lastVideo.cost.total_cost.toFixed(2)}</span>
        ) : null}
      </div>
      <video controls src={path} />
      <div className="output-footer">
        {lastVideo.prompt && (
          <>
            <div className="output-prompt output-prompt--hero">{lastVideo.prompt}</div>
            <CopyButton text={lastVideo.prompt} />
          </>
        )}
        <a href={path} target="_blank" rel="noreferrer">
          Open in new tab
        </a>
      </div>
    </div>
  );
}

function SequencePreview({ sessionId, outputs }) {
  const acceptedVideos = useMemo(
    () =>
      (outputs || []).filter(
        (o) => o.type === "video" && o.status !== "aborted",
      ),
    [outputs],
  );

  const videoRef = useRef(null);
  const indexRef = useRef(0);
  const playingRef = useRef(false);
  const [displayIndex, setDisplayIndex] = useState(0);

  function urlFor(idx) {
    const clip = acceptedVideos[idx];
    return clip
      ? `${API_BASE}/api/sessions/${sessionId}/files/${clip.path}`
      : "";
  }

  function loadClip(idx, autoPlay) {
    const vid = videoRef.current;
    if (!vid || idx < 0 || idx >= acceptedVideos.length) return;
    indexRef.current = idx;
    setDisplayIndex(idx);
    vid.src = urlFor(idx);
    vid.load();
    if (autoPlay) {
      const onReady = () => {
        vid.removeEventListener("canplay", onReady);
        vid.play().catch(() => {});
      };
      vid.addEventListener("canplay", onReady);
    }
  }

  function handleEnded() {
    const next = indexRef.current + 1;
    if (next < acceptedVideos.length) {
      loadClip(next, true);
    } else {
      playingRef.current = false;
    }
  }

  // Reset and load when session or accepted list changes
  React.useEffect(() => {
    indexRef.current = 0;
    setDisplayIndex(0);
    if (acceptedVideos.length > 0 && videoRef.current) {
      videoRef.current.src = urlFor(0);
      videoRef.current.load();
    }
  }, [sessionId]);

  React.useEffect(() => {
    if (acceptedVideos.length > 0 && videoRef.current) {
      const idx = Math.min(indexRef.current, acceptedVideos.length - 1);
      indexRef.current = idx;
      setDisplayIndex(idx);
      videoRef.current.src = urlFor(idx);
      videoRef.current.load();
    }
  }, [acceptedVideos.length]);

  if (acceptedVideos.length < 1) {
    return null;
  }

  return (
    <div className="sequence-preview">
      <div className="sequence-header">
        <span className="badge">Sequence Preview</span>
        <span className="part">
          Clip {displayIndex + 1} of {acceptedVideos.length}
        </span>
      </div>
      <video
        ref={videoRef}
        controls
        onEnded={handleEnded}
        onPlay={() => { playingRef.current = true; }}
        onPause={() => { playingRef.current = false; }}
      />
      <div className="sequence-controls">
        <button className="ghost-button small-button" disabled={displayIndex === 0} onClick={() => loadClip(0, false)}>⏮</button>
        <button className="ghost-button small-button" disabled={displayIndex === 0} onClick={() => loadClip(displayIndex - 1, false)}>◀ Prev</button>
        <button className="ghost-button small-button" disabled={displayIndex >= acceptedVideos.length - 1} onClick={() => loadClip(displayIndex + 1, false)}>Next ▶</button>
        <button className="ghost-button small-button" disabled={displayIndex >= acceptedVideos.length - 1} onClick={() => loadClip(acceptedVideos.length - 1, false)}>⏭</button>
      </div>
    </div>
  );
}

function ImagePreview({ src, className }) {
  const classes = ["image-preview", className].filter(Boolean).join(" ");
  return (
    <div className={classes}>
      {src ? (
        <img src={src} alt="Uploaded preview" />
      ) : (
        <div className="image-placeholder">Upload an image to preview it here.</div>
      )}
    </div>
  );
}

function GeneratorForm({
  onSubmit,
  disabled,
  apiKey,
  onApiKeyChange,
  imageFile,
  onImageChange,
  imagePreview,
  prompt,
  onPromptChange,
  duration,
  onDurationChange,
  resolution,
  onResolutionChange,
  preflight,
  onPreflightChange,
  refinePrompts,
  onRefinePromptsChange,
  autoAccept,
  onAutoAcceptChange,
  systemInstructions,
  onSystemInstructionsChange,
  apiHost,
  onApiHostChange,
  pricing,
  pricingLoading,
  pricingError,
  onRefreshPricing,
  estimatedClipCost,
  estimatedRunCost,
  ratePerSecond,
  preflightRate,
  violationFee,
  groundingText,
  onGroundingTextChange,
  onAnalyzeImage,
  analyzeBusy,
  title,
  onTitleChange,
  budgetCap,
  onBudgetCapChange,
}) {
  return (
    <form
      className="panel form-panel"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit({
          imageFile,
          prompt,
          apiKey,
          duration,
          resolution,
          preflight,
          refinePrompts,
          autoAccept,
          systemInstructions,
          apiHost,
          groundingText,
          title,
          budgetCap,
        });
      }}
    >
      <SectionHeader title="Generator" subtitle="Generate a video clip, then extend to build a sequence." />
      <div className="form-group">
        <label>
          API Key
          <input
            type="password"
            value={apiKey}
            onChange={(event) => onApiKeyChange(event.target.value)}
            placeholder="xai-..."
            required
            className="input-code"
          />
        </label>
      </div>

      <div className="form-group">
        <label>
          Session Title (Optional)
          <input
            type="text"
            value={title}
            onChange={(event) => onTitleChange(event.target.value)}
            placeholder="e.g., Sci-fi chase scene"
            className="input-text"
          />
        </label>
      </div>
      
      <div className="file-dropper">
        <label className="file-upload-label">
          <input
            type="file"
            accept="image/*"
            onChange={(event) => onImageChange(event.target.files?.[0] ?? null)}
            className="file-input-hidden"
            required={!imageFile}
          />
          <ImagePreview src={imagePreview} />
          {!imagePreview && <div className="upload-cta"><span>+</span> Upload Source Image</div>}
        </label>
      </div>

      <div className="input-group grounding-group">
        <div className="grounding-header">
          <span className="label-text">Continuity</span>
          <button
            type="button"
            className="ghost-button small-button"
            onClick={onAnalyzeImage}
            disabled={!imageFile || !apiKey || analyzeBusy}
            title={!apiKey ? "Enter API Key first" : "Use Grok to describe the image"}
          >
            {analyzeBusy ? "Analyzing..." : "Auto-describe image"}
          </button>
        </div>
        <textarea
          value={groundingText}
          onChange={(event) => onGroundingTextChange(event.target.value)}
          rows={3}
          placeholder="Describe characters, setting, and style to maintain consistency..."
          className="mono-textarea"
        />
      </div>

      <label className="input-group">
        <span className="label-text">Prompt</span>
        <textarea
          value={prompt}
          onChange={(event) => onPromptChange(event.target.value)}
          rows={4}
          required
          placeholder="Describe your video clip..."
        />
      </label>

      <div className="grid">
        <label className="input-group">
          <span className="label-text">Duration (sec)</span>
          <input
            type="number"
            min="1"
            max="15"
            value={duration}
            onChange={(event) => onDurationChange(Number(event.target.value))}
          />
        </label>
        <label className="input-group">
          <span className="label-text">Resolution</span>
          <select value={resolution} onChange={(event) => onResolutionChange(event.target.value)}>
            {resolutionOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="pricing-panel">
        <div className="pricing-info">
          <h4>Estimated Cost</h4>
          <div className="cost-breakdown">
             <div className="cost-item">
               <span>Rate</span>
               <strong>${ratePerSecond.toFixed(2)}/s</strong>
             </div>
             {preflight && (
               <div className="cost-item">
                 <span>Preflight</span>
                 <strong>+${preflightRate.toFixed(2)}</strong>
               </div>
             )}
             <div className="cost-item">
               <span style={{ color: "var(--error)" }}>Violation Fee</span>
               <strong style={{ color: "var(--error)" }}>${violationFee.toFixed(2)}</strong>
             </div>
          </div>
          <div className="total-estimates">
            <div className="estimate-row">
              <span>Per clip</span>
              <strong>{estimatedClipCost ? `$${estimatedClipCost.toFixed(2)}` : "--"}</strong>
            </div>
            <div className="estimate-row highlight">
              <span>Run total</span>
              <strong>{estimatedRunCost ? `$${estimatedRunCost.toFixed(2)}` : "--"}</strong>
            </div>
          </div>
          <div className="cost-item" style={{ marginTop: "12px", borderTop: "1px solid rgba(16, 185, 129, 0.1)", paddingTop: "12px" }}>
             <span>Budget Cap ($)</span>
             <input 
               type="number" 
               step="0.01" 
               min="0" 
               placeholder="Optional"
               value={budgetCap} 
               onChange={(e) => onBudgetCapChange(e.target.value)}
               className="input-code"
               style={{ width: "100%", marginTop: "4px", background: "rgba(0,0,0,0.2)", border: "1px solid rgba(16, 185, 129, 0.2)" }}
             />
          </div>
        </div>
        <div className="pricing-actions">
           {pricing?.last_loaded && (
            <span className="pricing-stamp">Updated {new Date(pricing.last_loaded).toLocaleTimeString()}</span>
          )}
          <button type="button" className="ghost-button icon-button" onClick={onRefreshPricing} title="Refresh Pricing">
            {pricingLoading ? "..." : "↻"}
          </button>
        </div>
        {pricingError && <div className="error-banner">{pricingError}</div>}
      </div>

      <div className="toggles-group">
        <label className="toggle">
          <input
            type="checkbox"
            checked={preflight}
            onChange={(event) => onPreflightChange(event.target.checked)}
          />
          <span className="toggle-slider"></span>
          <span className="toggle-label">Run preflight check</span>
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={refinePrompts}
            onChange={(event) => onRefinePromptsChange(event.target.checked)}
          />
          <span className="toggle-slider"></span>
          <span className="toggle-label">Refine with Grok</span>
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={autoAccept}
            onChange={(event) => onAutoAcceptChange(event.target.checked)}
            disabled={!refinePrompts}
          />
          <span className="toggle-slider"></span>
          <span className="toggle-label">Auto-accept refined</span>
        </label>
      </div>

      <details className="advanced-section">
        <summary>Advanced</summary>
        <div style={{ padding: "16px 0 0" }}>
          <div className="eyebrow" style={{ marginBottom: "16px" }}>Provider: xAI Grok Imagine Video</div>
          <div className="form-group">
            <label>
              API Host
              <input value={apiHost} onChange={(event) => onApiHostChange(event.target.value)} className="input-code" />
            </label>
          </div>
          <label className="input-group">
            <span className="label-text">System instructions</span>
            <textarea
              value={systemInstructions}
              onChange={(event) => onSystemInstructionsChange(event.target.value)}
              rows={3}
              className="mono-textarea"
            />
          </label>
        </div>
      </details>

      <button type="submit" className="primary-button" disabled={disabled || !imageFile}>
        {disabled ? "Processing..." : "Start Generation"}
      </button>
    </form>
  );
}

function SessionList({
  sessions,
  currentSessionId,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onDownloadArchive,
  onOpenFolder,
}) {
  if (!sessions?.length) {
    return (
      <div className="panel">
        <SectionHeader title="History" subtitle="Your generated sessions." />
        <div className="empty-panel">No history yet.</div>
      </div>
    );
  }

  return (
    <div className="panel session-list-panel">
      <SectionHeader title="History" subtitle="Previous runs" />
      <div className="session-list">
        {sessions.map((s) => (
          <button
            key={s.session_id}
            className={`session-item ${s.session_id === currentSessionId ? "active" : ""}`}
            onClick={() => onSelectSession(s.session_id)}
          >
            <div className="session-item-header">
              <span className="session-id" title={s.session_id}>
                {s.title || s.session_id}
              </span>
              <div className="session-controls">
                <button
                  className="ghost-button icon-button tiny-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenFolder(s.session_id);
                  }}
                  title="Open folder"
                >
                  📂
                </button>
                <button
                  className="ghost-button icon-button tiny-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDownloadArchive(s.session_id);
                  }}
                  title="Export ZIP"
                >
                  ⬇️
                </button>
                <button
                  className="ghost-button icon-button tiny-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    const newTitle = prompt("Rename session:", s.title || "");
                    if (newTitle !== null) {
                      onRenameSession(s.session_id, newTitle);
                    }
                  }}
                  title="Rename session"
                >
                  ✎
                </button>
                <button
                  className="ghost-button icon-button tiny-button"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm("Are you sure you want to delete this session?")) {
                      onDeleteSession(s.session_id);
                    }
                  }}
                  title="Delete session"
                  style={{ color: "var(--error)" }}
                >
                  🗑️
                </button>
                <span className={`status-badge ${s.status}`}>{s.status}</span>
              </div>
            </div>
            <div className="session-item-meta">
              <span>{new Date(s.created_at).toLocaleString()}</span>
              <span>
                {s.mode} • {s.total_parts} part{s.total_parts !== 1 ? "s" : ""}
              </span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function useAutoSave(session, enabled) {
  const downloadedRef = useRef(new Set());
  const lastSessionIdRef = useRef(null);

  React.useEffect(() => {
    if (!session) return;

    // If session changes, reset known files so we don't download history
    if (session.session_id !== lastSessionIdRef.current) {
      lastSessionIdRef.current = session.session_id;
      // Mark existing outputs as "seen" to prevent mass download of history
      session.outputs.forEach((o) => downloadedRef.current.add(o.path));
      return;
    }

    if (!enabled || !session?.outputs) return;

    session.outputs.forEach((output) => {
      if (output.type === "video" && !downloadedRef.current.has(output.path)) {
        downloadedRef.current.add(output.path);
        
        // Fetch as blob to force download behavior without navigation
        fetch(`${API_BASE}/api/sessions/${session.session_id}/files/${output.path}`)
          .then((resp) => resp.blob())
          .then((blob) => {
            const url = window.URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = output.path;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            window.URL.revokeObjectURL(url);
          })
          .catch((err) => console.error("Auto-save failed", err));
      }
    });
  }, [session, enabled]);
}

function App() {
  const [sessions, setSessions] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [session, setSession] = useState(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [apiKey, setApiKey] = useState(() => localStorage.getItem("grokv_api_key") || "");
  const [imageFile, setImageFile] = useState(null);
  const [imagePreview, setImagePreview] = useState("");
  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState(15);
  const [resolution, setResolution] = useState("720p");
  const [preflight, setPreflight] = useState(true);
  const [refinePrompts, setRefinePrompts] = useState(false);
  const [autoAccept, setAutoAccept] = useState(false);
  const [autoSave, setAutoSave] = useState(false);
  const [systemInstructions, setSystemInstructions] = useState(defaultSystemInstructions);
  const [apiHost, setApiHost] = useState(() => localStorage.getItem("grokv_api_host") || "api.x.ai");

  React.useEffect(() => {
    localStorage.setItem("grokv_api_key", apiKey);
  }, [apiKey]);

  React.useEffect(() => {
    localStorage.setItem("grokv_api_host", apiHost);
  }, [apiHost]);
  const [pricing, setPricing] = useState(null);
  const [pricingLoading, setPricingLoading] = useState(false);
  const [pricingError, setPricingError] = useState(null);
  const [groundingText, setGroundingText] = useState("");
  const [analyzeBusy, setAnalyzeBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [budgetCap, setBudgetCap] = useState("");

  useAutoSave(session, autoSave);

  const refinedInfo = useMemo(() => {
    if (!session?.refined_prompts?.length) {
      return null;
    }
    const index = session.refined_prompts.length - 1;
    return {
      refined: session.refined_prompts[index],
      original: session.prompts?.[index + 1] ?? "",
    };
  }, [session]);

  async function fetchSessions() {
    try {
      const response = await fetch(`${API_BASE}/api/sessions`);
      if (response.ok) {
        const data = await response.json();
        setSessions(data);
      }
    } catch (e) {
      console.error("Failed to load sessions", e);
    }
  }

  async function fetchSession(targetId) {
    const response = await fetch(`${API_BASE}/api/sessions/${targetId}`);
    if (!response.ok) {
      throw new Error("Unable to load session status.");
    }
    const data = await response.json();
    setSession(data);
    // Update the session in the list if it exists
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.session_id === targetId);
      if (idx === -1) return [data, ...prev];
      const next = [...prev];
      next[idx] = data;
      return next;
    });
  }

  async function handleRenameSession(targetId, newTitle) {
    try {
      const formData = new FormData();
      formData.append("title", newTitle);
      const response = await fetch(`${API_BASE}/api/sessions/${targetId}/title`, {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        throw new Error("Failed to update title");
      }
      const data = await response.json();
      // Update local state
      setSessions((prev) =>
        prev.map((s) => (s.session_id === targetId ? { ...s, title: data.title } : s))
      );
      if (session?.session_id === targetId) {
        setSession((prev) => ({ ...prev, title: data.title }));
      }
    } catch (err) {
      console.error("Failed to rename session", err);
      alert("Failed to rename session");
    }
  }

  async function handleDeleteSession(targetId) {
    try {
      const response = await fetch(`${API_BASE}/api/sessions/${targetId}`, {
        method: "DELETE",
      });
      if (!response.ok) {
        throw new Error("Failed to delete session");
      }
      setSessions((prev) => prev.filter((s) => s.session_id !== targetId));
      if (sessionId === targetId) {
        setSessionId(null);
        setSession(null);
      }
    } catch (err) {
      console.error("Failed to delete session", err);
      alert("Failed to delete session");
    }
  }

  async function handleDownloadArchive(targetId) {
    try {
      const url = `${API_BASE}/api/sessions/${targetId}/archive`;
      const link = document.createElement("a");
      link.href = url;
      link.download = `${targetId}_archive.zip`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    } catch (err) {
      console.error("Failed to export archive", err);
      alert("Failed to export archive");
    }
  }

  async function handleOpenFolder(targetId) {
    try {
      const response = await fetch(`${API_BASE}/api/sessions/${targetId}/open-folder`, {
        method: "POST",
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || "Failed to open folder.");
      }
    } catch (err) {
      console.error("Failed to open folder", err);
      alert(err.message || "Failed to open folder");
    }
  }

  async function submitAction(targetId, action) {
    await fetch(`${API_BASE}/api/sessions/${targetId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ action }),
    });
    fetchSession(targetId);

    // After regenerate, keep session active so user can edit prompt and re-submit
    if (action === "regenerate") {
      setSessionId(targetId);
    }

    // After submitting "extend", fetch last frame and set as seed image
    if (action === "extend") {
      try {
        const response = await fetch(`${API_BASE}/api/sessions/${targetId}/extend-frame`, {
          method: "POST",
        });
        if (!response.ok) {
          const data = await response.json();
          throw new Error(data.detail || "Unable to prepare frame.");
        }
        const data = await response.json();
        const frameResponse = await fetch(
          `${API_BASE}/api/sessions/${targetId}/files/${data.frame}`,
        );
        if (!frameResponse.ok) {
          throw new Error("Unable to load frame image.");
        }
        const blob = await frameResponse.blob();
        const file = new File([blob], data.frame, { type: blob.type || "image/png" });
        setImageFile(file);
        setSessionId(targetId);
      } catch (err) {
        setError(err.message || "Unable to extend from last frame.");
      }
    }
  }

  async function fetchPricing({ refresh = false } = {}) {
    setPricingError(null);
    if (refresh) {
      setPricingLoading(true);
    }
    try {
      const response = await fetch(
        refresh ? `${API_BASE}/api/pricing/refresh` : `${API_BASE}/api/pricing`,
        { method: refresh ? "POST" : "GET" },
      );
      if (!response.ok) {
        throw new Error("Unable to load pricing.");
      }
      const data = await response.json();
      setPricing(data);
    } catch (err) {
      setPricingError(err.message || "Unable to load pricing.");
    } finally {
      if (refresh) {
        setPricingLoading(false);
      }
    }
  }

  async function handleForkFromClip(targetSessionId, partIndex) {
    setError(null);
    try {
      const formData = new FormData();
      formData.append("part_index", String(partIndex));
      
      const response = await fetch(`${API_BASE}/api/sessions/${targetSessionId}/fork`, {
        method: "POST",
        body: formData,
      });
      
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || "Unable to fork session.");
      }
      
      const data = await response.json();
      const newSessionId = data.session_id;
      
      // Load the new session
      await fetchSession(newSessionId);
      setSessionId(newSessionId);
      
      // Load the frame image to prep for generation
      const frameResponse = await fetch(
        `${API_BASE}/api/sessions/${newSessionId}/files/${data.frame}`,
      );
      if (!frameResponse.ok) {
        throw new Error("Unable to load fork frame image.");
      }
      const blob = await frameResponse.blob();
      const file = new File([blob], data.frame, { type: blob.type || "image/png" });
      setImageFile(file);
      
      // Refresh list to show the new session
      fetchSessions();
      
    } catch (err) {
      console.error("Fork failed", err);
      setError(err.message || "Failed to fork session.");
    }
  }

  async function handleAnalyzeImage() {
    console.log("Analyze image clicked", { hasImage: !!imageFile, hasKey: !!apiKey });
    if (!imageFile || !apiKey) {
      setError("Please select an image and enter your API key first.");
      return;
    }
    setAnalyzeBusy(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append("image", imageFile);
      formData.append("api_key", apiKey);
      formData.append("api_host", apiHost);

      const response = await fetch(`${API_BASE}/api/analyze-image`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || "Failed to analyze image.");
      }

      const data = await response.json();
      setGroundingText(data.description);
    } catch (err) {
      setError(err.message || "Failed to analyze image.");
    } finally {
      setAnalyzeBusy(false);
    }
  }

  async function handleRunSubmit(payload) {
    setError(null);
    setIsSubmitting(true);
    try {
      if (!payload.apiKey) {
        throw new Error("API key is required.");
      }
      if (!payload.imageFile) {
        throw new Error("Image is required.");
      }
      if (!payload.prompt?.trim()) {
        throw new Error("Prompt is required.");
      }
      const body = new FormData();
      // Append auth fields first
      body.append("api_key", payload.apiKey);
      body.append("api_host", payload.apiHost);
      
      body.append("image", payload.imageFile);
      body.append("prompt", payload.prompt);
      body.append("duration", String(payload.duration));
      body.append("resolution", payload.resolution);
      body.append("preflight", String(payload.preflight));
      body.append("refine_prompts", String(payload.refinePrompts));
      body.append("refine_auto_accept", String(payload.autoAccept));
      body.append("system_instructions", payload.systemInstructions || "");
      body.append("grounding_text", payload.groundingText || "");
      if (payload.title) {
        body.append("title", payload.title);
      }
      // If we have an active session (e.g. extending), append to it
      if (sessionId) {
        body.append("session_id", sessionId);
      }

      const endpoint = `${API_BASE}/api/single`;
      const response = await fetch(endpoint, { method: "POST", body });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || "Failed to start run.");
      }
      const data = await response.json();
      setSessionId(data.session_id);
      await fetchSession(data.session_id);
      fetchSessions();
    } catch (err) {
      setError(err.message || "Failed to start run.");
    } finally {
      setIsSubmitting(false);
    }
  }

  React.useEffect(() => {
    if (!imageFile) {
      setImagePreview("");
      return undefined;
    }
    const url = URL.createObjectURL(imageFile);
    setImagePreview(url);
    return () => URL.revokeObjectURL(url);
  }, [imageFile]);

  React.useEffect(() => {
    fetchPricing();
    fetchSessions();
  }, []);

  React.useEffect(() => {
    if (!sessionId) {
      return;
    }
    const interval = setInterval(() => {
      fetchSession(sessionId).catch(() => {});
    }, 2000);
    return () => clearInterval(interval);
  }, [sessionId]);

  const pricingTable = pricing?.pricing?.models?.["grok-imagine-video"]?.per_second ?? {};
  const ratePerSecond = Number(pricingTable[resolution] ?? pricingTable["720p"] ?? 0.05);
  const preflightRate = Number(pricingTable["480p"] ?? ratePerSecond);
  const violationFee = Number(pricing?.pricing?.violation_fee ?? 0.05);
  const promptCount = 1;
  const perClipEstimate = preflight
    ? ratePerSecond * duration + preflightRate
    : ratePerSecond * duration;
  const estimatedClipCost = promptCount ? perClipEstimate : 0;
  const estimatedRunCost = promptCount ? perClipEstimate * promptCount : 0;

  return (
    <div className="app">
      <header>
        <div>
          <span className="eyebrow">Local Studio</span>
          <h1>Grok Video Studio</h1>
          <p>Generate cinematic clips with prompt refinement, preflight checks, and session playlists.</p>
        </div>
      </header>

      <main>
        <div className="grid-layout">
          <div className="sidebar">
            <GeneratorForm
              onSubmit={handleRunSubmit}
              disabled={isSubmitting}
              apiKey={apiKey}
              onApiKeyChange={setApiKey}
              imageFile={imageFile}
              onImageChange={setImageFile}
              imagePreview={imagePreview}
              prompt={prompt}
              onPromptChange={setPrompt}
              duration={duration}
              onDurationChange={setDuration}
              resolution={resolution}
              onResolutionChange={setResolution}
              preflight={preflight}
              onPreflightChange={setPreflight}
              refinePrompts={refinePrompts}
              onRefinePromptsChange={setRefinePrompts}
              autoAccept={autoAccept}
              onAutoAcceptChange={setAutoAccept}
              systemInstructions={systemInstructions}
              onSystemInstructionsChange={setSystemInstructions}
              apiHost={apiHost}
              onApiHostChange={setApiHost}
              pricing={pricing}
              pricingLoading={pricingLoading}
              pricingError={pricingError}
              onRefreshPricing={() => fetchPricing({ refresh: true })}
              estimatedClipCost={estimatedClipCost}
              estimatedRunCost={estimatedRunCost}
              ratePerSecond={ratePerSecond}
              preflightRate={preflightRate}
              violationFee={violationFee}
              groundingText={groundingText}
              onGroundingTextChange={setGroundingText}
              onAnalyzeImage={handleAnalyzeImage}
              analyzeBusy={analyzeBusy}
            />
            
            <div className="panel settings-panel">
               <label className="toggle">
                  <input
                    type="checkbox"
                    checked={autoSave}
                    onChange={(event) => setAutoSave(event.target.checked)}
                  />
                  <span className="toggle-slider"></span>
                  <span className="toggle-label">Auto-save generated videos</span>
               </label>
            </div>

            <SessionList
               sessions={sessions}
               currentSessionId={sessionId}
               onSelectSession={(id) => {
                 setSessionId(id);
                 fetchSession(id);
               }}
               onRenameSession={handleRenameSession}
               onDeleteSession={handleDeleteSession}
               onDownloadArchive={handleDownloadArchive}
               onOpenFolder={handleOpenFolder}
            />
          </div>

          <section className="panel status-panel">
            <SectionHeader
              title={session?.title || "Session"}
              subtitle={
                session?.parent_session_id
                  ? `ID: ${sessionId} • Forked from ${session.parent_session_id.slice(0, 6)}/P${session.parent_clip_index}`
                  : sessionId
                  ? `ID: ${sessionId}`
                  : "No session running yet."
              }
            />
            {error && <div className="error">{error}</div>}
            {!session && <div className="empty-panel">Start a run to see session info.</div>}
            {session && (
              <>
                {session.costs && (
                  <div className="pricing-summary">
                    <SectionHeader title="Costs" subtitle="Running session total" />
                    <div className="pricing-grid">
                      <div>
                        <span>Total spent</span>
                        <strong>${session.costs.total.toFixed(2)}</strong>
                      </div>
                      {(() => {
                        const violationTotal = session.costs.items?.reduce(
                          (sum, item) => sum + (item.penalty || 0),
                          0,
                        );
                        if (violationTotal > 0) {
                          return (
                            <div>
                              <span style={{ color: "var(--error)" }}>Violation fee</span>
                              <strong style={{ color: "var(--error)" }}>
                                ${violationTotal.toFixed(2)}
                              </strong>
                            </div>
                          );
                        }
                        return null;
                      })()}
                    </div>
                  </div>
                )}
                
                <StatusStepper session={session} />
                
                <div className="status-meta">
                  <div>
                    <span>Status</span>
                    <strong>{session.status}</strong>
                  </div>
                  <div>
                    <span>Progress</span>
                    <strong>
                      {session.current_part}/{session.total_parts}
                    </strong>
                  </div>
                </div>
                <div className="media-grid">
                  <div>
                    <SectionHeader title="Reference" subtitle="Uploaded image" />
                    <ImagePreview src={imagePreview} className="image-preview--session" />
                  </div>
                  <div>
                    <SectionHeader title="Latest Output" subtitle="Most recent render" />
                    <LatestOutput sessionId={session.session_id} outputs={session.outputs} />
                  </div>
                </div>
                <PlaylistLinks sessionId={session.session_id} playlists={session.playlists} />
                <PendingAction
                  sessionId={session.session_id}
                  pendingAction={session.pending_action}
                  allowedActions={session.allowed_actions}
                  onAction={submitAction}
                />
                <PromptDiff original={refinedInfo?.original} refined={refinedInfo?.refined} />
                <SectionHeader title="Outputs" subtitle="Preflight + final clips" />
                <OutputList 
                  sessionId={session.session_id} 
                  outputs={session.outputs} 
                  onFork={handleForkFromClip}
                />
                <SequencePreview sessionId={session.session_id} outputs={session.outputs} />
                <details className="advanced-section">
                  <summary>Logs ({session.logs.length} entries)</summary>
                  <div style={{ paddingTop: "16px" }}>
                    <LogPanel logs={session.logs} />
                  </div>
                </details>
              </>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}

export default App;
