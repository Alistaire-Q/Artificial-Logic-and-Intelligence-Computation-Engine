package com.hayatexe.alice;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import com.google.zxing.client.android.Intents;
import com.journeyapps.barcodescanner.ScanOptions;

import org.json.JSONObject;

/**
 * İlk açılış: bilgisayardaki ALICE'e bağlan. Bilgisayarda "alice phone" bir adres ve QR kod gösterir;
 * QR okutulur ya da adres yapıştırılır. Kaydetmeden önce bağlantı denenir.
 */
public class SetupActivity extends Activity {
    private static final int REQ_SCAN = 21;
    private static final int REQ_CAMERA = 22;
    private EditText field;
    private TextView status;
    private Button connect;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        CrashLog.install(this);
        int pad = dp(24);
        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(getColor(R.color.bg));
        sv.setFillViewport(true);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, dp(64), pad, pad);
        sv.addView(col);

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.mipmap.ic_launcher_round);
        col.addView(logo, new LinearLayout.LayoutParams(dp(64), dp(64)));

        TextView h = text(getIntent().getBooleanExtra("change", false) ? "Bağlantıyı değiştir" : "ALICE'e bağlan", 28, true, R.color.text);
        h.setPadding(0, dp(20), 0, dp(8));
        col.addView(h);
        col.addView(text("Bilgisayarda ALICE açıkken terminalde \"alice phone\" yaz (ya da Ayarlar'da telefonu aç). "
                + "Bir QR kod ve adres çıkar. QR'yi okut ya da adresi buraya yapıştır. Telefon ve bilgisayar aynı Wi-Fi'de olmalı.", 15, false, R.color.muted));

        Button scan = button("QR kodu okut", true);
        scan.setOnClickListener(v -> scan());
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp.topMargin = dp(28);
        col.addView(scan, lp);

        TextView or = text("ya da adresi yapıştır", 13, false, R.color.muted);
        or.setGravity(Gravity.CENTER);
        or.setPadding(0, dp(20), 0, dp(8));
        col.addView(or);

        field = new EditText(this);
        field.setHint("http://192.168.1.24:8765/m?t=…");
        field.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        field.setSingleLine(true);
        field.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        field.setTextColor(getColor(R.color.text));
        field.setHintTextColor(getColor(R.color.muted));
        field.setPadding(dp(14), dp(12), dp(14), dp(12));
        field.setBackground(round(R.color.card, R.color.line, 14));
        if (Prefs.configured(this)) field.setText(Prefs.appUrl(this));
        col.addView(field, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button paste = button("Panodan yapıştır", false);
        paste.setOnClickListener(v -> {
            ClipboardManager cm = getSystemService(ClipboardManager.class);
            ClipData c = cm == null ? null : cm.getPrimaryClip();
            if (c != null && c.getItemCount() > 0 && c.getItemAt(0).getText() != null) field.setText(c.getItemAt(0).getText());
        });
        LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48));
        lp2.topMargin = dp(8);
        col.addView(paste, lp2);

        connect = button("Bağlan", true);
        connect.setOnClickListener(v -> tryConnect(field.getText().toString()));
        LinearLayout.LayoutParams lp3 = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp3.topMargin = dp(16);
        col.addView(connect, lp3);

        status = text("", 14, false, R.color.muted);
        status.setPadding(0, dp(14), 0, 0);
        col.addView(status);
        setContentView(sv);
        CrashLog.showIfAny(this);
    }

    /** Kamera izni okuyucu açılmadan önce burada istenir: izinsiz açılan okuyucu bazı telefonlarda çöküyordu. */
    private void scan() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
            return;
        }
        startScanner();
    }

    private void startScanner() {
        try {
            ScanOptions o = new ScanOptions();
            o.setDesiredBarcodeFormats(ScanOptions.QR_CODE);
            o.setPrompt("Bilgisayardaki ALICE QR kodunu çerçeveye al");
            o.setBeepEnabled(false);
            o.setOrientationLocked(false);
            startActivityForResult(o.createScanIntent(this), REQ_SCAN);
        } catch (Exception e) {
            status.setText("QR okuyucu açılamadı (" + e.getClass().getSimpleName() + "). Adresi yapıştırarak bağlanabilirsin.");
        }
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        super.onRequestPermissionsResult(req, perms, results);
        if (req != REQ_CAMERA) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            startScanner();
        } else {
            status.setText("Kamera izni verilmedi. QR yerine bilgisayardaki adresi yapıştırarak bağlanabilirsin.");
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_SCAN || res != RESULT_OK || data == null) return;
        String text = data.getStringExtra(Intents.Scan.RESULT);
        if (text != null) {
            field.setText(text);
            tryConnect(text);
        }
    }

    /** Adresi dene: /api/state cevap verirse kaydet ve uygulamayı aç. */
    private void tryConnect(String raw) {
        String before = Prefs.configured(this) ? Prefs.appUrl(this) : "";
        if (!Prefs.saveFromUrl(this, raw)) {
            status.setText("Bu bir ALICE adresine benzemiyor. \"alice phone\"un gösterdiği adresin tamamını yapıştır (…/m?t=… ile biter).");
            return;
        }
        connect.setEnabled(false);
        status.setText("Bağlanıyor…");
        new Thread(() -> {
            String err = null;
            String host = "";
            try {
                JSONObject st = new JSONObject(Api.get(this, "/api/state"));
                if (st.optString("error").length() > 0) err = st.optString("error");
                host = st.optString("user");
            } catch (Exception e) {
                err = "Bilgisayara ulaşılamadı (" + e.getClass().getSimpleName() + "). Aynı Wi-Fi'de misin, ALICE açık mı, güvenlik duvarı izin veriyor mu?";
            }
            final String fErr = err, fHost = host;
            runOnUiThread(() -> {
                connect.setEnabled(true);
                if (fErr != null) {
                    status.setText(fErr);
                    if (!before.isEmpty()) Prefs.saveFromUrl(this, before); else Prefs.clear(this);
                    return;
                }
                status.setText("Bağlandı" + (fHost.isEmpty() ? "" : ": " + fHost));
                EventService.start(this);
                Intent i = new Intent(this, MainActivity.class);
                i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TASK | Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                finish();
            });
        }).start();
    }

    /* ---------------------------------------------------------------- küçük görünüm yardımcıları */
    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private TextView text(String s, int sp, boolean bold, int color) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(getColor(color));
        t.setLineSpacing(0, 1.2f);
        if (bold) t.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        return t;
    }

    private Button button(String s, boolean primary) {
        Button b = new Button(this);
        b.setText(s);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setStateListAnimator(null);
        b.setTextColor(primary ? 0xFFFFFFFF : getColor(R.color.text));
        b.setBackground(primary ? round(R.color.accent, R.color.accent, 14) : round(R.color.card, R.color.line, 14));
        return b;
    }

    private GradientDrawable round(int fill, int stroke, int radius) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(getColor(fill));
        g.setStroke(dp(1), getColor(stroke));
        g.setCornerRadius(dp(radius));
        return g;
    }
}
