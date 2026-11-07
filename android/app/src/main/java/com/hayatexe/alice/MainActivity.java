package com.hayatexe.alice;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.view.WindowManager;
import android.view.View;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Locale;

/**
 * ALICE telefon uygulaması: bilgisayardaki ALICE'in telefon arayüzünü (/m) tam ekran açar ve sayfaya
 * window.AliceNative köprüsünü verir (konuşma tanıma, titreşim, bildirim, tema, bağlantı ayarı).
 */
public class MainActivity extends Activity {
    private static final int REQ_FILE = 11, REQ_MIC = 12, REQ_NOTIFY = 13, REQ_LIVE_MIC = 14;

    private WebView web;
    private ValueCallback<Uri[]> fileCb;
    private SpeechRecognizer speech;
    private NativeMic mic;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean pageFailed;
    private TextToSpeech tts;
    private boolean ttsReady;
    private String pendingSpeech;
    private String pendingCall;  // bildirimden açılan arama: sayfa yüklenince sayfaya verilir

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        CrashLog.install(this);  // çökerse bir sonraki açılışta ayrıntısı gösterilir (CrashLog)
        if (handleLink(getIntent())) { /* yeni adres kaydedildi */ }
        if (!Prefs.configured(this)) {
            startActivity(new Intent(this, SetupActivity.class));
            finish();
            return;
        }
        setupWeb();
        askNotificationPermission();
        EventService.start(this);
        load(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (handleLink(intent) || web == null) { recreate(); return; }
        load(intent);
    }

    /** QR kodundaki http://ip:port/m?t=... ya da alice://connect bağlantısıyla açıldıysa adresi kaydet. */
    private boolean handleLink(Intent i) {
        if (i == null || !Intent.ACTION_VIEW.equals(i.getAction()) || i.getData() == null) return false;
        String before = Prefs.appUrl(this);
        return Prefs.saveFromUrl(this, i.getData().toString()) && !before.equals(Prefs.appUrl(this));
    }

    private void load(Intent i) {
        String callId = i != null ? i.getStringExtra("call_id") : null;
        if (callId != null) {
            Calls.cancel(this);
            if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(true); setTurnScreenOn(true); }
            try {
                pendingCall = new JSONObject().put("id", callId).put("action", i.getStringExtra("call_action")).toString();
            } catch (Exception ignored) { }
            i.removeExtra("call_id");
            if (web.getUrl() != null && web.getUrl().startsWith(Prefs.base(this)) && !pageFailed) { deliverCall(); return; }
        }
        String tab = i != null ? i.getStringExtra("tab") : null;
        if (tab != null && web.getUrl() != null && web.getUrl().startsWith(Prefs.base(this))) {
            web.evaluateJavascript("window.show && show(" + JSONObject.quote(tab) + ")", null);
            return;
        }
        pageFailed = false;
        // Evdeysek bilgisayara doğrudan, değilsek aktarma sunucusu üzerinden (Prefs.pick ağ isteği yapar).
        new Thread(() -> {
            Prefs.pick(this);
            String url = Prefs.appUrl(this) + (tab != null ? "#" + tab : "");
            ui.post(() -> { if (web != null) web.loadUrl(url); });
        }, "alice-pick").start();
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private void setupWeb() {
        web = new WebView(this);
        web.setBackgroundColor(getColor(R.color.bg));
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setTextZoom(100);
        s.setUserAgentString(s.getUserAgentString() + " ALICEApp/" + BuildConfigVersion.name(this));
        web.addJavascriptInterface(new Bridge(), "AliceNative");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                // ALICE'in kendi sayfaları uygulamada, dış bağlantılar tarayıcıda açılır.
                if (u.toString().startsWith(Prefs.base(MainActivity.this))) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception ignored) { }
                return true;
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                if (url != null && url.startsWith(Prefs.base(MainActivity.this))) ui.postDelayed(MainActivity.this::deliverCall, 600);
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (r.isForMainFrame()) showOffline(String.valueOf(e.getDescription()));
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (fileCb != null) fileCb.onReceiveValue(null);
                fileCb = cb;
                try {
                    Intent pick = p.createIntent();
                    if (p.isCaptureEnabled() && p.getAcceptTypes().length > 0 && p.getAcceptTypes()[0].startsWith("audio")) {
                        pick = new Intent(android.provider.MediaStore.Audio.Media.RECORD_SOUND_ACTION);
                    }
                    startActivityForResult(pick, REQ_FILE);
                } catch (Exception ex) {
                    try { startActivityForResult(p.createIntent(), REQ_FILE); } catch (Exception ex2) { fileCb = null; return false; }
                }
                return true;
            }

            @Override
            public void onPermissionRequest(PermissionRequest r) {
                // Sayfa mikrofon isterse (tarayıcı yolu) izin uygulamanın iznine bağlı.
                ui.post(() -> {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        r.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                    } else {
                        r.deny();
                        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
                    }
                });
            }
        });
    }

    private void deliverCall() {
        if (pendingCall == null || web == null) return;
        String js = "window.aliceCallIntent && aliceCallIntent(" + pendingCall + ")";
        pendingCall = null;
        web.evaluateJavascript(js, null);
    }

    /** Görüşmede bilgisayar ses üretemezse ALICE'in cümlesini telefonun kendi sesi okur. */
    private void speakNow(String text) {
        if (tts == null) {
            pendingSpeech = text;
            tts = new TextToSpeech(this, st -> {
                ttsReady = st == TextToSpeech.SUCCESS;
                if (ttsReady) {
                    tts.setLanguage(new Locale("tr", "TR"));
                    tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                        @Override public void onStart(String id) { }
                        @Override public void onDone(String id) { spoke(); }
                        @Override public void onError(String id) { spoke(); }
                    });
                }
                String t = pendingSpeech;
                pendingSpeech = null;
                if (t != null) speakNow(t);
            });
            return;
        }
        if (!ttsReady) { spoke(); return; }
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "alice-" + System.nanoTime());
    }

    private void spoke() {
        ui.post(() -> { if (web != null) web.evaluateJavascript("window.aliceSpoke && aliceSpoke()", null); });
    }

    /** Bilgisayara ulaşılamadı: uygulamanın içindeki küçük hata sayfası (yeniden dene / adresi değiştir). */
    private void showOffline(String why) {
        if (pageFailed) return;
        pageFailed = true;
        String host = Uri.parse(Prefs.base(this)).getHost();
        web.loadUrl("file:///android_asset/offline.html?host=" + Uri.encode(host == null ? "" : host) + "&why=" + Uri.encode(why));
        web.getSettings().setAllowFileAccess(true);
    }

    @Override
    public void onBackPressed() {
        if (web == null) { super.onBackPressed(); return; }
        web.evaluateJavascript("window.aliceBack ? String(window.aliceBack()) : 'false'", v -> {
            if (!"\"true\"".equals(v) && !"true".equals(v)) moveTaskToBack(true);
        });
    }

    /** Uygulama önde mi: güncelleme onayı doğrudan açılabilir mi, bildirimle mi sorulmalı. */
    static volatile boolean visible;

    @Override
    protected void onPause() {
        visible = false;
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        visible = true;
        Updater.check(this, false);
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.cancel(EventService.ID_ALERT_GROUP);
        if (web != null) web.evaluateJavascript("document.dispatchEvent(new Event('visibilitychange'))", null);
    }

    @Override
    protected void onDestroy() {
        if (speech != null) speech.destroy();
        if (mic != null) mic.stop();
        if (tts != null) tts.shutdown();
        Calls.ring(this, false);
        if (web != null) web.destroy();
        super.onDestroy();
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_FILE || fileCb == null) return;
        Uri[] out = null;
        if (res == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                ArrayList<Uri> list = new ArrayList<>();
                for (int i = 0; i < data.getClipData().getItemCount(); i++) list.add(data.getClipData().getItemAt(i).getUri());
                out = list.toArray(new Uri[0]);
            } else if (data.getData() != null) {
                out = new Uri[]{data.getData()};
            }
        }
        fileCb.onReceiveValue(out);
        fileCb = null;
    }

    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
        }
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req == REQ_MIC && res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) startListening(lastLang);
        else if (req == REQ_MIC) js("aliceVoice", "{\"type\":\"error\",\"text\":\"Mikrofon izni verilmedi\"}");
        if (req == REQ_LIVE_MIC) js("aliceMicPermission", res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED ? "true" : "false");
        if (req == REQ_NOTIFY) EventService.start(this);
    }

    private void js(String fn, String json) {
        ui.post(() -> { if (web != null) web.evaluateJavascript("window." + fn + " && window." + fn + "(" + json + ")", null); });
    }

    /* ---------------------------------------------------------------- konuşma tanıma */
    private String lastLang = "tr-TR";

    private void startListening(String lang) {
        lastLang = lang == null || lang.isEmpty() ? "tr-TR" : lang;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            js("aliceVoice", "{\"type\":\"error\",\"text\":\"Telefonda konuşma tanıma yok (Google uygulaması gerekli)\"}");
            return;
        }
        if (speech != null) speech.destroy();
        speech = SpeechRecognizer.createSpeechRecognizer(this);
        speech.setRecognitionListener(new RecognitionListener() {
            @Override public void onReadyForSpeech(Bundle b) { }
            @Override public void onBeginningOfSpeech() { }
            @Override public void onRmsChanged(float rms) {
                float level = Math.max(0f, Math.min(1f, (rms + 2f) / 12f));
                js("aliceVoice", "{\"type\":\"level\",\"level\":" + level + "}");
            }
            @Override public void onBufferReceived(byte[] b) { }
            @Override public void onEndOfSpeech() { }
            @Override public void onError(int err) {
                String msg = err == SpeechRecognizer.ERROR_NO_MATCH || err == SpeechRecognizer.ERROR_SPEECH_TIMEOUT
                        ? "Bir şey duyamadım" : err == SpeechRecognizer.ERROR_NETWORK || err == SpeechRecognizer.ERROR_NETWORK_TIMEOUT
                        ? "Konuşma tanıma için internet gerekli" : "Ses anlaşılamadı (" + err + ")";
                js("aliceVoice", "{\"type\":\"error\",\"text\":" + JSONObject.quote(msg) + "}");
                js("aliceVoice", "{\"type\":\"end\"}");
            }
            @Override public void onResults(Bundle b) {
                ArrayList<String> r = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                String text = r != null && !r.isEmpty() ? r.get(0) : "";
                js("aliceVoice", "{\"type\":\"final\",\"text\":" + JSONObject.quote(text) + "}");
                js("aliceVoice", "{\"type\":\"end\"}");
            }
            @Override public void onPartialResults(Bundle b) {
                ArrayList<String> r = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                if (r != null && !r.isEmpty()) js("aliceVoice", "{\"type\":\"partial\",\"text\":" + JSONObject.quote(r.get(0)) + "}");
            }
            @Override public void onEvent(int t, Bundle b) { }
        });
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, lastLang);
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        i.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, 1800L);
        speech.startListening(i);
    }

    /* ---------------------------------------------------------------- sayfaya açılan köprü */
    private class Bridge {
        @JavascriptInterface public void listen(String lang) { ui.post(() -> startListening(lang)); }
        @JavascriptInterface public void stopListening() { ui.post(() -> { if (speech != null) speech.stopListening(); }); }
        @JavascriptInterface public void cancelListening() { ui.post(() -> { if (speech != null) speech.cancel(); }); }
        /** Canlı arama mikrofonu: parçalar window.aliceMic({pcm, lv}) ile gelir. İzin yoksa false (izin istenir). */
        @JavascriptInterface public boolean micStart() {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                ui.post(() -> requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_LIVE_MIC));
                return false;
            }
            if (mic == null) mic = new NativeMic(MainActivity.this);
            return mic.start((b64, peak) -> js("aliceMic", "{\"pcm\":\"" + b64 + "\",\"lv\":" + peak + "}"));
        }
        @JavascriptInterface public void micStop() { if (mic != null) mic.stop(); }

        @JavascriptInterface public void vibrate(int ms) {
            Vibrator v = getSystemService(Vibrator.class);
            if (v != null && v.hasVibrator()) v.vibrate(VibrationEffect.createOneShot(Math.max(5, Math.min(ms, 400)), VibrationEffect.DEFAULT_AMPLITUDE));
        }

        @JavascriptInterface public void notify(String title, String body, String tag) {
            EventService.notifyAlert(MainActivity.this, title, body, tag, "bell", null);
        }

        @JavascriptInterface public void ring(boolean on) { Calls.ring(MainActivity.this, on); }
        @JavascriptInterface public void speak(String text) { ui.post(() -> speakNow(text)); }
        @JavascriptInterface public void stopSpeaking() { ui.post(() -> { if (tts != null) tts.stop(); }); }
        @JavascriptInterface public void keepScreen(boolean on) {
            ui.post(() -> {
                if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }

        @JavascriptInterface public void setBadge(int n) { /* başlatıcı rozeti bildirim sayısından gelir */ }

        @JavascriptInterface public void reconfigure() {
            ui.post(() -> {
                Intent i = new Intent(MainActivity.this, SetupActivity.class);
                i.putExtra("change", true);
                startActivity(i);
            });
        }

        @JavascriptInterface public String version() { return BuildConfigVersion.name(MainActivity.this); }
        @JavascriptInterface public long versionCode() { return Updater.versionCode(MainActivity.this); }
        @JavascriptInterface public void checkUpdate() { Updater.check(MainActivity.this, true); }

        @JavascriptInterface public String network() {
            ConnectivityManager cm = getSystemService(ConnectivityManager.class);
            NetworkCapabilities nc = cm == null ? null : cm.getNetworkCapabilities(cm.getActiveNetwork());
            if (nc == null) return "ağ yok";
            if (nc.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return "VPN";
            if (nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "Wi-Fi";
            if (nc.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) return "mobil veri";
            return "bağlı";
        }

        @JavascriptInterface public void setTheme(String t) {
            ui.post(() -> {
                boolean nightSys = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
                boolean dark = "dark".equals(t) || (!"light".equals(t) && nightSys);
                Window w = getWindow();
                int bg = dark ? Color.parseColor("#0A0A0B") : Color.parseColor("#F6F6F7");
                w.setStatusBarColor(bg);
                w.setNavigationBarColor(bg);
                if (web != null) web.setBackgroundColor(bg);
                View d = w.getDecorView();
                int f = d.getSystemUiVisibility();
                f = dark ? f & ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR & ~View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
                         : f | View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
                d.setSystemUiVisibility(f);
            });
        }

        // Çevrimdışı sayfasından: yeniden dene.
        @JavascriptInterface public void retry() {
            new Thread(() -> {
                Prefs.pick(MainActivity.this);
                ui.post(() -> { pageFailed = false; web.getSettings().setAllowFileAccess(false); web.loadUrl(Prefs.appUrl(MainActivity.this)); });
            }, "alice-pick").start();
        }
    }

    /** Sürüm adı (BuildConfig'e bağımlı olmadan). */
    static final class BuildConfigVersion {
        static String name(Context c) {
            try { return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName; } catch (Exception e) { return "?"; }
        }
    }
}
