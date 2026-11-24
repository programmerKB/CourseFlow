import { useEffect, useLayoutEffect, useRef, useState } from "react";

const MAX_SECONDS = 20;
const MAX_BYTES = 5 * 1024 * 1024;
const FORMATS = ["audio/webm;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/ogg;codecs=opus"];
const FILE_TYPES: Record<string, string> = {
  webm: "audio/webm",
  m4a: "audio/mp4",
  mp4: "audio/mp4",
  ogg: "audio/ogg",
  oga: "audio/ogg",
  wav: "audio/wav",
  mp3: "audio/mpeg",
};

type Recording = {
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
  startedAt: number;
  timer: number;
  send: boolean;
};

type AudioClip = { audio: Blob; fileName?: string };

function release(stream: MediaStream) {
  stream.getTracks().forEach((track) => track.stop());
}

export default function ChatVoice({
  disabled,
  onSend,
}: {
  disabled: boolean;
  onSend: (blob: Blob) => void;
}) {
  const [pending, setPending] = useState(false);
  const [recording, setRecording] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [clip, setClip] = useState<AudioClip | null>(null);
  const [error, setError] = useState("");
  const active = useRef<Recording | null>(null);
  const permissionRequest = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const supported = window.isSecureContext && typeof navigator.mediaDevices?.getUserMedia === "function"
    && typeof window.MediaRecorder === "function";

  function stop(send: boolean) {
    const capture = active.current;
    if (!capture || capture.recorder.state === "inactive") return;
    capture.send = send;
    window.clearTimeout(capture.timer);
    capture.recorder.stop();
    release(capture.stream);
  }

  function clearCapture() {
    permissionRequest.current++;
    const capture = active.current;
    active.current = null;
    if (!capture) return;
    window.clearTimeout(capture.timer);
    if (capture.recorder.state !== "inactive") capture.recorder.stop();
    release(capture.stream);
  }

  useLayoutEffect(() => clearCapture, []);
  useEffect(() => {
    if (!recording) return;
    const clock = window.setInterval(() => {
      const capture = active.current;
      if (capture) setElapsed(Math.min(MAX_SECONDS, Math.floor((performance.now() - capture.startedAt) / 1000)));
    }, 250);
    return () => window.clearInterval(clock);
  }, [recording]);

  function validateSize(blob: Blob) {
    if (!blob.size || blob.size > MAX_BYTES) {
      setError(blob.size ? "每段音訊最多 5 MiB，請縮短後重試。" : "音訊檔案是空的，請重新錄音或選擇其他檔案。");
      return false;
    }
    return true;
  }

  function submit(blob: Blob) {
    if (!validateSize(blob)) return;
    setClip(null);
    setError("");
    onSend(blob);
  }

  function selectAudio(file?: File) {
    if (!file) return;
    setError("");
    if (!validateSize(file)) return;
    const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
    const mediaType = FILE_TYPES[extension];
    if (!file.name.includes(".") || !Object.hasOwn(FILE_TYPES, extension)) {
      setError("請選擇 WebM、M4A、MP4、Ogg、WAV 或 MP3 音檔。");
      return;
    }
    setClip({ audio: file.slice(0, file.size, mediaType), fileName: file.name });
  }

  async function start() {
    setError("");
    if (!supported) return;
    const mimeType = FORMATS.find((format) => MediaRecorder.isTypeSupported(format));
    if (!mimeType) {
      setError("此瀏覽器沒有支援的錄音格式。請選擇音檔或使用文字輸入。");
      return;
    }
    let stream: MediaStream | undefined;
    setPending(true);
    const request = ++permissionRequest.current;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (permissionRequest.current !== request) {
        release(stream);
        return;
      }
      const recorder = new MediaRecorder(stream, { mimeType });
      const capture: Recording = {
        recorder, stream, chunks: [], startedAt: performance.now(), timer: 0, send: false,
      };
      active.current = capture;
      recorder.ondataavailable = (event) => {
        if (event.data.size) capture.chunks.push(event.data);
      };
      recorder.onerror = () => {
        if (active.current !== capture) return;
        clearCapture();
        setRecording(false);
        setError("錄音中斷，請重新錄音或改用文字輸入。");
      };
      recorder.onstop = () => {
        release(capture.stream);
        if (active.current !== capture) return;
        window.clearTimeout(capture.timer);
        active.current = null;
        setRecording(false);
        const blob = new Blob(capture.chunks, { type: recorder.mimeType || mimeType });
        if (capture.send) submit(blob);
        else setClip({ audio: blob });
      };
      recorder.start();
      // Stop slightly before 20 s to allow the final encoded audio frame.
      capture.timer = window.setTimeout(() => stop(false), MAX_SECONDS * 1000 - 100);
      setElapsed(0);
      setRecording(true);
    } catch (failure) {
      if (stream) release(stream);
      if (permissionRequest.current !== request) return;
      active.current = null;
      const denied = failure instanceof DOMException && failure.name === "NotAllowedError";
      setError(denied
        ? "麥克風未獲授權。請允許麥克風權限或選擇音檔。"
        : "無法開啟麥克風。請選擇音檔或使用文字輸入。");
    } finally {
      if (permissionRequest.current === request) setPending(false);
    }
  }

  function cancel() {
    clearCapture();
    setPending(false);
    setRecording(false);
    setClip(null);
    setError("");
  }

  return (
    <div className="voice-controls">
      <input
        ref={fileInput}
        type="file"
        aria-label="音檔"
        accept={Object.keys(FILE_TYPES).map((extension) => `.${extension}`).join(",")}
        hidden
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          selectAudio(file);
        }}
      />
      {!recording && !clip && (
        <>
          {supported && (
            <button type="button" disabled={disabled || pending} onClick={start}>
              {pending ? "等待麥克風權限…" : "開始錄音"}
            </button>
          )}
          <button type="button" disabled={disabled || pending} onClick={() => fileInput.current?.click()}>
            選擇音檔
          </button>
          {!supported && (
            <small>{window.isSecureContext ? "可選擇裝置上的錄音檔。" : "可先用裝置錄音，再選擇音檔。使用 HTTPS 可在網頁直接錄音。"}</small>
          )}
        </>
      )}
      {(recording || clip || pending) && (
        <>
          {recording && <span aria-label={`錄音中，${elapsed} 秒`}><span className="recording-dot" aria-hidden="true" /> {elapsed} 秒</span>}
          {clip && <span className="voice-file" role="status">{clip.fileName ?? "錄音已停止"}</span>}
          {(recording || clip) && (
            <button type="button" disabled={disabled} onClick={() => clip ? submit(clip.audio) : stop(true)}>
              {clip?.fileName ? "送出音檔" : clip ? "送出錄音" : "停止並送出"}
            </button>
          )}
          <button type="button" onClick={cancel}>{clip?.fileName ? "取消音檔" : "取消錄音"}</button>
        </>
      )}
      <small>每段最多 20 秒、5 MiB。送出後在本系統伺服器以 Whisper 轉寫，錄音不會傳到外部服務。</small>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
