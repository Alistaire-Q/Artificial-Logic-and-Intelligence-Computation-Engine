package com.hayatexe.alice;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.util.Base64;

/**
 * Canlı arama mikrofonu: 16 kHz PCM16, 120 ms'lik parçalar. Sayfa ev ağında düz http ile açıldığından WebView
 * getUserMedia vermiyor; mikrofonu uygulama açar, parçaları sayfaya verir. Ses kaynağı VOICE_COMMUNICATION ve
 * arama modu: telefonun yankı engelleyicisi ALICE'in sesini mikrofondan siler, kullanıcı konuşunca araya girebilir.
 */
final class NativeMic {
    interface Sink {
        void chunk(String b64, float peak);
    }

    private static final int RATE = 16000;
    private static final int CHUNK = 1920; // örnek: 120 ms

    private final Context ctx;
    private volatile boolean on;
    private int oldMode = AudioManager.MODE_NORMAL;
    private boolean oldSpeaker;

    NativeMic(Context ctx) {
        this.ctx = ctx.getApplicationContext();
    }

    synchronized boolean start(Sink sink) {
        if (on) return true;
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        final AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(min, CHUNK * 2 * 4));
        } catch (Exception e) {
            return false;
        }
        if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
            rec.release();
            return false;
        }
        AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
        if (am != null) {
            oldMode = am.getMode();
            oldSpeaker = am.isSpeakerphoneOn();
            try {
                am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                am.setSpeakerphoneOn(true);
            } catch (Exception ignored) { }
        }
        int sid = rec.getAudioSessionId();
        final AcousticEchoCanceler aec = AcousticEchoCanceler.isAvailable() ? AcousticEchoCanceler.create(sid) : null;
        final NoiseSuppressor ns = NoiseSuppressor.isAvailable() ? NoiseSuppressor.create(sid) : null;
        final AutomaticGainControl agc = AutomaticGainControl.isAvailable() ? AutomaticGainControl.create(sid) : null;
        try { if (aec != null) aec.setEnabled(true); } catch (Exception ignored) { }
        try { if (ns != null) ns.setEnabled(true); } catch (Exception ignored) { }
        try { if (agc != null) agc.setEnabled(true); } catch (Exception ignored) { }
        on = true;
        Thread t = new Thread(() -> {
            short[] buf = new short[CHUNK];
            byte[] out = new byte[CHUNK * 2];
            try {
                rec.startRecording();
                while (on) {
                    int n = 0;
                    while (on && n < CHUNK) {
                        int r = rec.read(buf, n, CHUNK - n);
                        if (r <= 0) break;
                        n += r;
                    }
                    if (n <= 0) break;
                    int peak = 0;
                    for (int i = 0; i < n; i++) {
                        short v = buf[i];
                        int a = Math.abs((int) v);
                        if (a > peak) peak = a;
                        out[2 * i] = (byte) (v & 0xff);
                        out[2 * i + 1] = (byte) ((v >> 8) & 0xff);
                    }
                    sink.chunk(Base64.encodeToString(out, 0, n * 2, Base64.NO_WRAP), peak / 32768f);
                }
            } catch (Exception ignored) {
                // mikrofon başka uygulamada ya da kayıt durdu
            } finally {
                try { rec.stop(); } catch (Exception ignored) { }
                rec.release();
                if (aec != null) aec.release();
                if (ns != null) ns.release();
                if (agc != null) agc.release();
                on = false;
            }
        }, "alice-mic");
        t.start();
        return true;
    }

    synchronized void stop() {
        if (!on) return;
        on = false;
        AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
        if (am != null) {
            try {
                am.setSpeakerphoneOn(oldSpeaker);
                am.setMode(oldMode);
            } catch (Exception ignored) { }
        }
    }
}
