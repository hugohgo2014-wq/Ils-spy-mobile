#!/usr/bin/env python3
"""Adds an app-private Termux bootstrap, first-run installer and persistent shell service."""
from pathlib import Path
import xml.etree.ElementTree as ET
import sys

root = Path(sys.argv[1]).resolve()
pkg = "com.ilspy.mobile"
java = root / "app/src/main/java/com/ilspy/mobile"
manifest = root / "app/src/main/AndroidManifest.xml"
workbench = java / "WorkbenchActivity.java"
assets = root / "app/src/main/assets/termux"
assets.mkdir(parents=True, exist_ok=True)

for path in (manifest, workbench):
    if not path.is_file():
        raise SystemExit(f"Required file missing: {path}")

# The bootstrap is built specifically for this application id, so its hard-coded
# Termux prefix resolves to this app's private files/usr directory.
installer = r'''package com.ilspy.mobile;

import android.content.Context;
import android.system.Os;
import java.io.*;
import java.util.*;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

final class TermuxBootstrapInstaller {
    interface Progress { void update(String message); }
    private TermuxBootstrapInstaller() {}

    static File prefix(Context context) { return new File(context.getFilesDir(), "usr"); }

    static void install(Context context, Progress progress) throws Exception {
        File prefix = prefix(context);
        if (new File(prefix, "bin/bash").isFile()
                && new File(prefix, "bin/apt").exists()
                && new File(prefix, "bin/clang").exists()
                && new File(prefix, "bin/cmake").exists()) {
            progress.update("Ambiente Termux já instalado · verificando ferramentas");
            return;
        }

        String arch = null;
        for (String abi : android.os.Build.SUPPORTED_ABIS) {
            if ("arm64-v8a".equals(abi)) { arch = "aarch64"; break; }
            if ("x86_64".equals(abi)) { arch = "x86_64"; break; }
        }
        if (arch == null) throw new IOException("Arquitetura não suportada: "
                + Arrays.toString(android.os.Build.SUPPORTED_ABIS));

        File staging = new File(context.getFilesDir(), "usr-staging");
        deleteTree(staging);
        if (!staging.mkdirs() && !staging.isDirectory())
            throw new IOException("Não foi possível criar diretório de instalação.");
        ArrayList<String[]> symlinks = new ArrayList<>();
        byte[] buffer = new byte[65536];
        int entries = 0;
        progress.update("Extraindo ambiente Termux (" + arch + ")");

        try (InputStream raw = context.getAssets().open("termux/bootstrap-" + arch + ".zip");
             ZipInputStream zip = new ZipInputStream(new BufferedInputStream(raw))) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                String name = entry.getName();
                if (name.equals("SYMLINKS.txt")) {
                    BufferedReader reader = new BufferedReader(new InputStreamReader(zip, "UTF-8"));
                    String line;
                    while ((line = reader.readLine()) != null) {
                        String[] parts = line.split("←", 2);
                        if (parts.length != 2) throw new IOException("Entrada SYMLINKS.txt inválida: " + line);
                        symlinks.add(parts);
                    }
                } else {
                    File target = new File(staging, name);
                    String canonical = target.getCanonicalPath();
                    if (!canonical.startsWith(staging.getCanonicalPath() + File.separator)
                            && !canonical.equals(staging.getCanonicalPath()))
                        throw new IOException("Caminho inseguro no bootstrap: " + name);
                    if (entry.isDirectory()) {
                        if (!target.mkdirs() && !target.isDirectory())
                            throw new IOException("Falha criando " + name);
                    } else {
                        File parent = target.getParentFile();
                        if (!parent.mkdirs() && !parent.isDirectory())
                            throw new IOException("Falha criando diretório " + parent);
                        try (FileOutputStream out = new FileOutputStream(target)) {
                            int count;
                            while ((count = zip.read(buffer)) != -1) out.write(buffer, 0, count);
                        }
                        if (name.startsWith("bin/") || name.startsWith("libexec/")
                                || name.startsWith("lib/apt/apt-helper")
                                || name.startsWith("lib/apt/methods/")) {
                            Os.chmod(target.getAbsolutePath(), 0700);
                        }
                    }
                }
                entries++;
                if ((entries % 250) == 0) progress.update("Extraindo arquivos · " + entries);
                zip.closeEntry();
            }
        }

        if (symlinks.isEmpty()) throw new IOException("Bootstrap sem SYMLINKS.txt.");
        progress.update("Configurando links e permissões");
        for (String[] link : symlinks) {
            File linkFile = new File(staging, link[1]);
            File parent = linkFile.getParentFile();
            if (!parent.mkdirs() && !parent.isDirectory())
                throw new IOException("Falha criando diretório do link: " + link[1]);
            Os.symlink(link[0], linkFile.getAbsolutePath());
        }

        deleteTree(prefix);
        if (!staging.renameTo(prefix)) throw new IOException("Não foi possível finalizar a instalação do ambiente.");
        File home = new File(context.getFilesDir(), "home");
        if (!home.exists() && !home.mkdirs()) throw new IOException("Não foi possível criar HOME.");
        File tmp = new File(context.getCacheDir(), "termux-tmp");
        if (!tmp.exists() && !tmp.mkdirs()) throw new IOException("Não foi possível criar TMPDIR.");

        String[] required = {"bin/bash", "bin/apt", "bin/clang", "bin/cmake", "bin/git", "bin/python"};
        for (String item : required) {
            if (!new File(prefix, item).exists())
                throw new IOException("Ferramenta ausente no bootstrap: " + item);
        }
        progress.update("Ambiente instalado e validado");
    }

    private static void deleteTree(File file) throws IOException {
        if (!file.exists()) return;
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) for (File child : children) deleteTree(child);
        }
        if (!file.delete()) throw new IOException("Não foi possível remover " + file);
    }
}
'''
(java / "TermuxBootstrapInstaller.java").write_text(installer, encoding="utf-8")

service = r'''package com.ilspy.mobile;

import android.app.*;
import android.content.*;
import android.os.*;
import java.io.*;
import java.util.*;

public final class TerminalService extends Service {
    private static final String CHANNEL = "ilspy_terminal";
    private static final int NOTIFICATION_ID = 7311;
    private final LocalBinder binder = new LocalBinder();
    private final StringBuilder pendingOutput = new StringBuilder();
    private Process shell;
    private BufferedWriter input;
    private Thread reader;
    private final Object outputLock = new Object();

    public final class LocalBinder extends Binder {
        TerminalService service() { return TerminalService.this; }
    }

    @Override public void onCreate() {
        super.onCreate();
        createChannel();
        Intent open = new Intent(this, TerminalActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_terminal)
                .setContentTitle("ILSpy Mobile · Terminal ativo")
                .setContentText("O ambiente de compilação continua em execução")
                .setContentIntent(pi).setOngoing(true).build();
        startForeground(NOTIFICATION_ID, notification);
        startShell();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL,
                    "Terminal e compilação", NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }

    private synchronized void startShell() {
        if (shell != null && shell.isAlive()) return;
        try {
            File prefix = TermuxBootstrapInstaller.prefix(this);
            File bash = new File(prefix, "bin/bash");
            if (!bash.isFile()) throw new IOException("Bootstrap Termux não instalado.");
            File home = new File(getFilesDir(), "home");
            File tmp = new File(getCacheDir(), "termux-tmp");
            ProcessBuilder builder = new ProcessBuilder(bash.getAbsolutePath(), "--login");
            builder.directory(home);
            builder.redirectErrorStream(true);
            Map<String, String> env = builder.environment();
            env.put("HOME", home.getAbsolutePath());
            env.put("PREFIX", prefix.getAbsolutePath());
            env.put("TMPDIR", tmp.getAbsolutePath());
            env.put("PATH", prefix.getAbsolutePath() + "/bin:" + prefix.getAbsolutePath() + "/bin/applets:/system/bin");
            env.put("LD_LIBRARY_PATH", prefix.getAbsolutePath() + "/lib");
            env.put("LANG", "C.UTF-8");
            env.put("TERM", "xterm-256color");
            env.put("COLORTERM", "truecolor");
            shell = builder.start();
            input = new BufferedWriter(new OutputStreamWriter(shell.getOutputStream(), "UTF-8"));
            appendOutput("ILSpy Mobile · Termux shell iniciado\n");
            reader = new Thread(() -> readShell(shell), "ILSpy-Terminal-Reader");
            reader.setDaemon(true);
            reader.start();
        } catch (Exception error) {
            appendOutput("ERRO AO INICIAR TERMINAL: " + error + "\n");
        }
    }

    private void readShell(Process process) {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(process.getInputStream(), "UTF-8"))) {
            String line;
            while ((line = reader.readLine()) != null) appendOutput(line + "\n");
            appendOutput("\n[Shell encerrado]\n");
        } catch (Exception error) { appendOutput("\n[Leitura encerrada: " + error + "]\n"); }
    }

    private void appendOutput(String value) {
        synchronized (outputLock) {
            pendingOutput.append(value);
            if (pendingOutput.length() > 120000) pendingOutput.delete(0, pendingOutput.length() - 90000);
        }
        Intent update = new Intent("com.ilspy.mobile.TERMINAL_OUTPUT");
        update.setPackage(getPackageName());
        sendBroadcast(update);
    }

    public void execute(String command) {
        synchronized (this) {
            try {
                if (shell == null || !shell.isAlive()) startShell();
                if (input == null) { appendOutput("Shell indisponível.\n"); return; }
                input.write(command);
                input.newLine();
                input.flush();
            } catch (Exception error) { appendOutput("ERRO: " + error + "\n"); }
        }
    }

    public String drainOutput() {
        synchronized (outputLock) {
            String value = pendingOutput.toString();
            pendingOutput.setLength(0);
            return value;
        }
    }

    @Override public IBinder onBind(Intent intent) { return binder; }
    @Override public int onStartCommand(Intent intent, int flags, int startId) { return START_STICKY; }

    @Override public void onDestroy() {
        try { if (input != null) input.close(); } catch (Exception ignored) {}
        if (shell != null) shell.destroy();
        super.onDestroy();
    }
}
'''
(java / "TerminalService.java").write_text(service, encoding="utf-8")

terminal_activity = r'''package com.ilspy.mobile;

import android.app.*;
import android.content.*;
import android.os.*;
import android.graphics.Color;
import android.view.*;
import android.view.inputmethod.EditorInfo;
import android.widget.*;

public final class TerminalActivity extends Activity {
    private TerminalService service;
    private boolean bound;
    private TextView output;
    private EditText command;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override public void run() {
            if (service != null) {
                String text = service.drainOutput();
                if (!text.isEmpty()) {
                    output.append(text);
                    if (output.length() > 100000) output.setText(output.getText().subSequence(output.length()-70000, output.length()));
                    ((ScrollView)output.getParent().getParent()).post(() -> ((ScrollView)output.getParent().getParent()).fullScroll(View.FOCUS_DOWN));
                }
            }
            handler.postDelayed(this, 250);
        }
    };
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((TerminalService.LocalBinder)binder).service();
            handler.removeCallbacks(refresh); handler.post(refresh);
        }
        @Override public void onServiceDisconnected(ComponentName name) { service = null; }
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(12,18,30));
        getWindow().setNavigationBarColor(Color.rgb(12,18,30));
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(12,12,12,12);
        root.setBackgroundColor(Color.rgb(12,18,30));
        TextView title = new TextView(this);
        title.setText("ILSpy Mobile  /  Termux");
        title.setTextColor(Color.WHITE); title.setTextSize(18); title.setPadding(4,4,4,12);
        root.addView(title);
        ScrollView scroll = new ScrollView(this);
        output = new TextView(this);
        output.setTextColor(Color.rgb(180,235,205));
        output.setTextSize(12);
        output.setTypeface(android.graphics.Typeface.MONOSPACE);
        output.setText("Conectando ao shell…\n");
        output.setPadding(10,10,10,10);
        scroll.addView(output);
        root.addView(scroll, new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        command = new EditText(this);
        command.setSingleLine(true); command.setTextColor(Color.WHITE);
        command.setHintTextColor(Color.GRAY); command.setHint("Digite um comando…");
        command.setTextSize(14); command.setImeOptions(EditorInfo.IME_ACTION_SEND);
        row.addView(command, new LinearLayout.LayoutParams(0, -2, 1));
        Button send = new Button(this); send.setText("Executar"); send.setAllCaps(false);
        row.addView(send);
        root.addView(row);
        setContentView(root);
        Runnable submit = () -> {
            String value = command.getText().toString();
            if (!value.trim().isEmpty() && service != null) {
                output.append("$ " + value + "\n");
                service.execute(value);
                command.setText("");
            }
        };
        send.setOnClickListener(v -> submit.run());
        command.setOnEditorActionListener((v, action, event) -> {
            if (action == EditorInfo.IME_ACTION_SEND || (event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER)) {
                submit.run(); return true;
            }
            return false;
        });
        Intent serviceIntent = new Intent(this, TerminalService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(serviceIntent); else startService(serviceIntent);
        bindService(serviceIntent, connection, Context.BIND_AUTO_CREATE);
    }

    @Override protected void onDestroy() {
        handler.removeCallbacks(refresh);
        if (bound) unbindService(connection);
        super.onDestroy();
    }
}
'''
(java / "TerminalActivity.java").write_text(terminal_activity, encoding="utf-8")

startup = r'''package com.ilspy.mobile;

import android.app.*;
import android.content.*;
import android.graphics.Color;
import android.os.*;
import android.view.Gravity;
import android.widget.*;

public final class BootstrapActivity extends Activity {
    private TextView status;
    private ProgressBar progress;
    private boolean started;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(10,16,28));
        getWindow().setNavigationBarColor(Color.rgb(10,16,28));
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(32,32,32,32);
        root.setBackgroundColor(Color.rgb(10,16,28));
        TextView brand = new TextView(this);
        brand.setText("ILSpy Mobile");
        brand.setTextColor(Color.WHITE); brand.setTextSize(28); brand.setGravity(Gravity.CENTER);
        root.addView(brand, new LinearLayout.LayoutParams(-1,-2));
        TextView subtitle = new TextView(this);
        subtitle.setText("Preparando ambiente de desenvolvimento");
        subtitle.setTextColor(Color.rgb(150,165,185)); subtitle.setTextSize(13);
        subtitle.setGravity(Gravity.CENTER); subtitle.setPadding(0,8,0,28);
        root.addView(subtitle);
        progress = new ProgressBar(this);
        progress.setIndeterminate(true);
        root.addView(progress, new LinearLayout.LayoutParams(-1,-2));
        status = new TextView(this);
        status.setTextColor(Color.rgb(100,220,175)); status.setTextSize(13);
        status.setGravity(Gravity.CENTER); status.setPadding(0,20,0,0);
        root.addView(status);
        setContentView(root);
        prepare();
    }

    private void prepare() {
        if (started) return;
        started = true;
        new Thread(() -> {
            try {
                TermuxBootstrapInstaller.install(this, message -> runOnUiThread(() -> status.setText(message)));
                runOnUiThread(() -> {
                    status.setText("Iniciando serviços…");
                    Intent service = new Intent(this, TerminalService.class);
                    if (Build.VERSION.SDK_INT >= 26) startForegroundService(service); else startService(service);
                    new Handler(Looper.getMainLooper()).postDelayed(() -> {
                        startActivity(new Intent(this, WorkbenchActivity.class));
                        finish();
                    }, 350);
                });
            } catch (Throwable error) {
                runOnUiThread(() -> {
                    progress.setVisibility(ProgressBar.GONE);
                    status.setText("Falha ao preparar o ambiente:\n" + error);
                    Button retry = new Button(this); retry.setText("Tentar novamente");
                    ((LinearLayout)findViewById(android.R.id.content).getChildAt(0)).addView(retry);
                    retry.setOnClickListener(v -> { started = false; prepare(); });
                });
            }
        }, "ILSpy-Bootstrap-Setup").start();
    }
}
'''
(java / "BootstrapActivity.java").write_text(startup, encoding="utf-8")

# Add a terminal entry point to the existing workbench toolbar.
text = workbench.read_text(encoding="utf-8")
anchor = 'toolbar.addView(actionButton("Abrir", true, this::chooseFile));'
if anchor not in text:
    raise SystemExit("Could not find Workbench toolbar insertion point.")
if 'TerminalActivity.class' not in text:
    text = text.replace(anchor, anchor + '\n        toolbar.addView(actionButton("Terminal", false, () -> startActivity(new android.content.Intent(this, TerminalActivity.class))));', 1)
workbench.write_text(text, encoding="utf-8")

# Make the bootstrap screen the launcher and register the persistent foreground service.
ET.register_namespace("android", "http://schemas.android.com/apk/res/android")
tree = ET.parse(manifest)
app = tree.getroot().find("application")
if app is None: raise SystemExit("AndroidManifest.xml has no application element")
ANDROID = "{http://schemas.android.com/apk/res/android}"
launcher_theme = None
for activity in list(app.findall("activity")):
    for filt in list(activity.findall("intent-filter")):
        actions = [x.get(ANDROID+"name") for x in filt.findall("action")]
        categories = [x.get(ANDROID+"name") for x in filt.findall("category")]
        if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories:
            launcher_theme = activity.get(ANDROID+"theme")
            activity.remove(filt)

startup_activity = None
for activity in app.findall("activity"):
    if activity.get(ANDROID+"name") == ".BootstrapActivity": startup_activity = activity
if startup_activity is None:
    startup_activity = ET.SubElement(app, "activity")
    startup_activity.set(ANDROID+"name", ".BootstrapActivity")
    startup_activity.set(ANDROID+"exported", "true")
    startup_activity.set(ANDROID+"configChanges", "keyboardHidden|orientation|screenSize")
    if launcher_theme: startup_activity.set(ANDROID+"theme", launcher_theme)
    filt = ET.SubElement(startup_activity, "intent-filter")
    ET.SubElement(filt, "action").set(ANDROID+"name", "android.intent.action.MAIN")
    ET.SubElement(filt, "category").set(ANDROID+"name", "android.intent.category.LAUNCHER")

terminal_activity_node = None
for activity in app.findall("activity"):
    if activity.get(ANDROID+"name") == ".TerminalActivity": terminal_activity_node = activity
if terminal_activity_node is None:
    terminal_activity_node = ET.SubElement(app, "activity")
    terminal_activity_node.set(ANDROID+"name", ".TerminalActivity")
    terminal_activity_node.set(ANDROID+"exported", "false")
    terminal_activity_node.set(ANDROID+"configChanges", "keyboardHidden|orientation|screenSize")

service_node = None
for service_node_candidate in app.findall("service"):
    if service_node_candidate.get(ANDROID+"name") == ".TerminalService": service_node = service_node_candidate
if service_node is None:
    service_node = ET.SubElement(app, "service")
    service_node.set(ANDROID+"name", ".TerminalService")
    service_node.set(ANDROID+"exported", "false")
    service_node.set(ANDROID+"stopWithTask", "false")
    service_node.set(ANDROID+"foregroundServiceType", "specialUse")
    prop = ET.SubElement(service_node, "property")
    prop.set(ANDROID+"name", "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE")
    prop.set(ANDROID+"value", "Terminal integrado para compilação e análise de código solicitada pelo usuário")

existing_permissions = {p.get(ANDROID+"name") for p in tree.getroot().findall("uses-permission")}
for permission in ("android.permission.FOREGROUND_SERVICE", "android.permission.FOREGROUND_SERVICE_SPECIAL_USE", "android.permission.POST_NOTIFICATIONS"):
    if permission not in existing_permissions:
        node = ET.Element("uses-permission")
        node.set(ANDROID+"name", permission)
        tree.getroot().insert(0, node)

tree.write(manifest, encoding="utf-8", xml_declaration=True)
print("Termux bootstrap installer, first-run screen, terminal UI and foreground shell service added.")
