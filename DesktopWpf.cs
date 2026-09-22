using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Media;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

class Desktop {
    readonly string root = AppDomain.CurrentDomain.BaseDirectory;
    readonly string data;
    readonly bool smoke, capture;
    readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 };
    Window window;
    Forms.NotifyIcon tray;
    DispatcherTimer timer;
    Dictionary<string, object> runtime, lastState, settings;
    List<TaskRow> taskRows = new List<TaskRow>();
    List<string> selectedIds = new List<string>();
    bool initializing = true, polling, quitting, loaded, changingSwitch;
    string voiceSignature = "", historySignature = "";
    Process service;

    T C<T>(string name) where T : FrameworkElement { return (T)window.FindName(name); }
    static Dictionary<string, object> Map(object o) { return o as Dictionary<string, object> ?? new Dictionary<string, object>(); }
    static string Str(Dictionary<string, object> m, string k, string d = "") { return m.ContainsKey(k) && m[k] != null ? Convert.ToString(m[k]) : d; }
    static bool Bool(Dictionary<string, object> m, string k) { return m.ContainsKey(k) && Convert.ToBoolean(m[k]); }
    static int Num(Dictionary<string, object> m, string k, int d = 0) { return m.ContainsKey(k) ? Convert.ToInt32(m[k]) : d; }
    static IEnumerable<object> Items(object o) { var a = o as IEnumerable; if (a != null) foreach (object x in a) yield return x; }
    static Brush Brush(string value) { return (Brush)new BrushConverter().ConvertFromString(value); }
    void Notice(string message) { C<TextBlock>("Notice").Text = message; }
    static string DataPath { get { return Environment.GetEnvironmentVariable("CODEX_PHONE_DATA") ?? Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "data"); } }
    static void Trace(string message) {
        try { Directory.CreateDirectory(Path.Combine(DataPath, "logs")); File.AppendAllText(Path.Combine(DataPath, "logs", "desktop.log"), DateTime.Now.ToString("s") + " " + message + Environment.NewLine); } catch { }
    }

    Desktop(bool smokeTest, bool captureUi) {
        smoke = smokeTest; capture = captureUi; data = DataPath;
        Directory.CreateDirectory(data);
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("CodexPhone.UI.xaml")) window = (Window)XamlReader.Load(stream);
        try { window.Icon = BitmapFrame.Create(new Uri(Path.Combine(root, "phone.ico"))); } catch { }
        RenderOptions.SetBitmapScalingMode(window, BitmapScalingMode.HighQuality);
        TextOptions.SetTextFormattingMode(window, TextFormattingMode.Display);
        C<Button>("CloseWindow").Click += delegate { window.Hide(); };
        C<Button>("MinimizeWindow").Click += delegate { window.WindowState = WindowState.Minimized; };
        C<Button>("MaximizeWindow").Click += delegate { Maximize(); };
        C<Grid>("DragSurface").MouseLeftButtonDown += delegate(object sender, MouseButtonEventArgs e) {
            if (e.OriginalSource is System.Windows.Shapes.Ellipse || e.OriginalSource is Button) return;
            if (e.ClickCount == 2) Maximize(); else if (e.LeftButton == MouseButtonState.Pressed) window.DragMove();
        };
        string[] navs = { "NavVoice", "NavTasks", "NavConnect", "NavHistory" };
        for (int i = 0; i < navs.Length; i++) { int index = i; C<RadioButton>(navs[i]).Checked += delegate { Navigate(index); }; }
        C<CheckBox>("EnabledSwitch").Checked += async delegate { await SaveSwitch(); };
        C<CheckBox>("EnabledSwitch").Unchecked += async delegate { await SaveSwitch(); };
        C<RadioButton>("BackendSapi").Checked += delegate { EngineChanged(); };
        C<RadioButton>("BackendEdge").Checked += delegate { EngineChanged(); };
        foreach (string name in new[] { "Rate", "Pitch", "Volume", "Ring" }) C<Slider>(name).ValueChanged += delegate { SoundLabels(); Dirty(); };
        foreach (string name in new[] { "Report", "CodexHome", "Extension", "Caller", "LocalIP", "SipPort" }) C<TextBox>(name).TextChanged += delegate { SoundLabels(); Dirty(); };
        C<ComboBox>("Scope").SelectionChanged += delegate { C<ListBox>("Tasks").IsEnabled = C<ComboBox>("Scope").SelectedIndex == 1; Dirty(); };
        C<ComboBox>("Voices").SelectionChanged += delegate { Dirty(); };
        C<ComboBox>("AnswerMode").SelectionChanged += delegate { Dirty(); };
        C<CheckBox>("WorkBuddySwitch").Checked += delegate { Dirty(); };
        C<CheckBox>("WorkBuddySwitch").Unchecked += delegate { Dirty(); };
        foreach (string name in new[] { "SaveVoice", "SaveTasks", "SaveConnect" }) Bind(name, SaveAll);
        Bind("PreviewVoice", async delegate { Notice("正在准备试听…"); var result = Map(await Api("/preview", Collect())); new SoundPlayer(Str(result, "path")).Play(); Notice("正在试听当前文案与音色。试听不会拨打电话。"); });
        Bind("TestCall", async delegate { await SaveAll(); await Api("/test", new Dictionary<string, object>()); Notice("测试电话已排队，请留意话机。手动测试不受通知开关限制。"); });
        Bind("RefreshVoices", async delegate { await Api("/refresh-voices", new Dictionary<string, object>()); voiceSignature = ""; Notice("正在刷新音色列表…"); });
        Bind("RefreshTasks", RefreshTasks);
        Bind("ResetSound", delegate { foreach (string name in new[] { "Rate", "Pitch", "Volume" }) C<Slider>(name).Value = 0; SoundLabels(); Dirty(); return Task.FromResult(0); });
        Bind("CopyWorkBuddy", delegate { Clipboard.SetText(WorkBuddyPrompt()); Notice("已复制配置提示，可以交给 WorkBuddy 保存。"); return Task.FromResult(0); });
        Bind("OpenReadme", delegate { Open(Path.Combine(root, "README.md")); return Task.FromResult(0); });
        Bind("OpenData", delegate { Open(data); return Task.FromResult(0); });
        Bind("Firewall", delegate {
            int sipPort = Convert.ToInt32(C<TextBox>("SipPort").Text);
            Process.Start(new ProcessStartInfo("powershell.exe", "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(root, "setup-firewall.ps1") + "\" -SipPort " + sipPort) { UseShellExecute = true, Verb = "runas", WindowStyle = ProcessWindowStyle.Hidden });
            Notice("已请求管理员权限。结果保存在数据目录的 firewall-result.txt。"); return Task.FromResult(0);
        });
        tray = new Forms.NotifyIcon { Text = "Agent Call", Visible = !smoke };
        try { tray.Icon = new System.Drawing.Icon(Path.Combine(root, "phone.ico")); } catch { tray.Icon = System.Drawing.SystemIcons.Information; }
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("打开窗口", null, delegate { Show(); });
        menu.Items.Add("开启 / 关闭通知", null, delegate { C<CheckBox>("EnabledSwitch").IsChecked = !(C<CheckBox>("EnabledSwitch").IsChecked == true); });
        menu.Items.Add("退出程序", null, async delegate { await Quit(); });
        tray.ContextMenuStrip = menu; tray.DoubleClick += delegate { Show(); };
        window.Closing += delegate(object s, System.ComponentModel.CancelEventArgs e) { if (!quitting) { e.Cancel = true; window.Hide(); } };
        window.Closed += delegate { tray.Visible = false; tray.Dispose(); };
        timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1.8) }; timer.Tick += async delegate { await Poll(); };
        window.Loaded += async delegate {
            try {
                Trace("Modern desktop loaded"); await StartService(); await Poll(); await RefreshTasks(); timer.Start();
                if (!loaded) throw new Exception("无法读取程序设置，请查看日志。");
                if (smoke) await SmokeTest();
                else if (capture) { await Task.Delay(1500); Capture(Path.Combine(data, "desktop-preview.png")); }
            } catch (Exception ex) { Error(ex); if (smoke) { File.WriteAllText(Path.Combine(data, "desktop-smoke.txt"), "FAILED: " + ex); var ignored = Quit(); } }
        };
    }

    void Bind(string name, Func<Task> action) {
        Button button = C<Button>(name);
        button.Click += async delegate { button.IsEnabled = false; try { await action(); } catch (Exception ex) { Error(ex); } finally { button.IsEnabled = true; } };
    }
    void Open(string path) { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
    void Show() { window.Show(); if (window.WindowState == WindowState.Minimized) window.WindowState = WindowState.Normal; window.Activate(); }
    void Maximize() { window.MaxHeight = SystemParameters.WorkArea.Height; window.WindowState = window.WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized; }
    void Dirty() { if (loaded && !initializing) Notice("有未保存的更改 · 试听可以直接使用当前设置。"); }
    void Navigate(int index) {
        string[] pages = { "VoicePage", "TasksPage", "ConnectPage", "HistoryPage" };
        string[] titles = { "声音与播报", "通知与任务", "连接设置", "通知记录" };
        string[] subtitles = { "一句熟悉的提醒，一种喜欢的声音。", "只让值得留意的完成，响起一通电话。", "连接好一次，之后放心交给它。", "每一通提醒，都有迹可循。" };
        for (int i = 0; i < pages.Length; i++) C<StackPanel>(pages[i]).Visibility = i == index ? Visibility.Visible : Visibility.Collapsed;
        C<TextBlock>("PageTitle").Text = titles[index]; C<TextBlock>("PageSubtitle").Text = subtitles[index]; C<ScrollViewer>("PageScroll").ScrollToTop();
    }
    bool Edge { get { return C<RadioButton>("BackendEdge").IsChecked == true; } }
    void EngineChanged() {
        if (!initializing) { C<ComboBox>("Voices").Text = ""; voiceSignature = ""; FillVoices(); Dirty(); }
        C<Slider>("Pitch").IsEnabled = C<Slider>("Volume").IsEnabled = Edge;
        C<Grid>("PitchGroup").Opacity = C<Grid>("VolumeGroup").Opacity = Edge ? 1 : .5;
        C<TextBlock>("EngineHint").Text = Edge ? "滑动调节，试听一下。数值修改后点击保存即可生效。" : "本地语音可调语速；切换 Edge 在线可调音调与音量。";
    }
    string Delta(double value, string suffix) { int v = (int)Math.Round(value); return (v > 0 ? "+" : "") + v + suffix; }
    void SoundLabels() {
        C<TextBlock>("RateValue").Text = Delta(C<Slider>("Rate").Value, "%");
        C<TextBlock>("PitchValue").Text = Delta(C<Slider>("Pitch").Value, " Hz");
        C<TextBlock>("VolumeValue").Text = ((int)Math.Round(C<Slider>("Volume").Value) + 100) + "%";
        C<TextBlock>("RingValue").Text = (int)Math.Round(C<Slider>("Ring").Value) + " 秒";
        C<TextBlock>("CharacterCount").Text = C<TextBox>("Report").Text.Length + " / 1000";
    }
    string VoiceName() { var box = C<ComboBox>("Voices"); var item = box.SelectedItem as VoiceItem; return item != null && box.Text == item.ToString() ? item.Id : box.Text.Trim(); }
    List<string> CheckedIds() {
        var ids = new List<string>(); foreach (TaskRow row in taskRows) if (row.Selected) ids.Add(row.Id);
        foreach (string old in selectedIds) if (!taskRows.Exists(r => r.Id == old) && !ids.Contains(old)) ids.Add(old);
        return ids;
    }
    Dictionary<string, object> Collect() {
        int sipPort; if (!int.TryParse(C<TextBox>("SipPort").Text.Trim(), out sipPort)) throw new Exception("SIP 端口需要填写数字。");
        return new Dictionary<string, object> {
            {"enabled",C<CheckBox>("EnabledSwitch").IsChecked==true},{"workbuddy_enabled",C<CheckBox>("WorkBuddySwitch").IsChecked==true},
            {"scope",C<ComboBox>("Scope").SelectedIndex==0?"all":"selected"},{"selected_threads",CheckedIds()},
            {"report_line",C<TextBox>("Report").Text},{"backend",Edge?"edge":"sapi"},{"voice",VoiceName()},
            {"rate",(int)Math.Round(C<Slider>("Rate").Value)},{"pitch",(int)Math.Round(C<Slider>("Pitch").Value)},
            {"volume",(int)Math.Round(C<Slider>("Volume").Value)},{"ring_seconds",(int)Math.Round(C<Slider>("Ring").Value)},
            {"codex_home",C<TextBox>("CodexHome").Text.Trim()},{"extension",C<TextBox>("Extension").Text.Trim()},
            {"caller",C<TextBox>("Caller").Text.Trim()},{"advertise_ip",C<TextBox>("LocalIP").Text.Trim()},
            {"sip_port",sipPort},{"auto_answer_mode",new string[]{"two_stage","header","manual"}[C<ComboBox>("AnswerMode").SelectedIndex]}
        };
    }
    async Task SaveAll() {
        var values = Collect(); var result = Map(await Api("/settings", values)); selectedIds = CheckedIds();
        Notice(Bool(result, "restart_required") ? "设置已保存 · 网络设置需要退出并重新启动程序。" : "已保存，下次来电会使用这些设置。"); await Poll();
    }
    async Task SaveSwitch() {
        PaintSwitch(); if (!loaded || initializing || changingSwitch) return; changingSwitch = true;
        bool value = C<CheckBox>("EnabledSwitch").IsChecked == true;
        C<CheckBox>("EnabledSwitch").IsEnabled = false;
        try { await Api("/settings", new Dictionary<string, object> { { "enabled", value } }); Notice(value ? "电话通知已开启。" : "电话通知已暂停，等待中的自动来电已取消。"); }
        catch (Exception ex) { initializing = true; C<CheckBox>("EnabledSwitch").IsChecked = !value; initializing = false; PaintSwitch(); Error(ex); }
        finally { changingSwitch = false; C<CheckBox>("EnabledSwitch").IsEnabled = true; }
    }
    void PaintSwitch() {
        bool on = C<CheckBox>("EnabledSwitch").IsChecked == true;
        C<TextBlock>("SwitchCaption").Text = on ? "已开启，完成时轻轻提醒。" : "已暂停，享受片刻安静。";
        if (tray != null) tray.Text = on ? "Agent Call · 通知已开启" : "Agent Call · 通知已关闭";
    }

    async Task<object> Api(string path, object body = null) {
        return await Task.Run(delegate {
            if (runtime == null) throw new Exception("电话服务尚未连接。");
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + Str(runtime, "port") + path); req.Proxy = null; req.Timeout = 180000;
            req.Headers["X-Phone-Token"] = Str(runtime, "token");
            if (body != null) { req.Method = "POST"; req.ContentType = "application/json"; byte[] raw = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(body)); req.ContentLength = raw.Length; using (var stream = req.GetRequestStream()) stream.Write(raw, 0, raw.Length); }
            try { using (var res = req.GetResponse()) using (var reader = new StreamReader(res.GetResponseStream(), Encoding.UTF8)) return new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 }.DeserializeObject(reader.ReadToEnd()); }
            catch (WebException ex) { if (ex.Response != null) using (var reader = new StreamReader(ex.Response.GetResponseStream())) { var error = Map(new JavaScriptSerializer().DeserializeObject(reader.ReadToEnd())); throw new Exception(Str(error, "error", ex.Message)); } throw; }
        });
    }
    async Task StartService() {
        string run = Path.Combine(data, "runtime.json");
        if (File.Exists(run)) { try { runtime = Map(json.DeserializeObject(File.ReadAllText(run))); await Api("/state"); return; } catch { runtime = null; } }
        string exe = Path.Combine(root, "PhoneService.exe");
        if (!File.Exists(exe)) throw new Exception("缺少 PhoneService.exe。请完整解压发布包，不要只复制窗口程序。");
        var info = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = root }; info.EnvironmentVariables["CODEX_PHONE_DATA"] = data;
        service = Process.Start(info);
        for (int i = 0; i < 80; i++) { await Task.Delay(250); if (service.HasExited) throw new Exception("电话服务未能启动，请检查 data/logs/app.log。"); if (File.Exists(run)) { try { runtime = Map(json.DeserializeObject(File.ReadAllText(run))); await Api("/state"); return; } catch { runtime = null; } } }
        throw new Exception("服务启动超时，请查看日志。");
    }
    async Task Poll() {
        if (polling) return; polling = true;
        try {
            lastState = Map(await Api("/state")); settings = Map(lastState["settings"]);
            if (!loaded) {
                initializing = true;
                C<CheckBox>("EnabledSwitch").IsChecked = Bool(settings, "enabled"); C<CheckBox>("WorkBuddySwitch").IsChecked = Bool(settings, "workbuddy_enabled");
                C<TextBox>("Report").Text = Str(settings, "report_line"); C<RadioButton>(Str(settings, "backend") == "edge" ? "BackendEdge" : "BackendSapi").IsChecked = true;
                C<ComboBox>("Voices").Text = Str(settings, "voice"); C<Slider>("Rate").Value = Num(settings, "rate"); C<Slider>("Pitch").Value = Num(settings, "pitch"); C<Slider>("Volume").Value = Num(settings, "volume"); C<Slider>("Ring").Value = Num(settings, "ring_seconds", 4);
                C<ComboBox>("Scope").SelectedIndex = Str(settings, "scope") == "selected" ? 1 : 0;
                C<ComboBox>("AnswerMode").SelectedIndex = Str(settings, "auto_answer_mode") == "manual" ? 2 : Str(settings, "auto_answer_mode") == "header" ? 1 : 0;
                C<TextBox>("CodexHome").Text = Str(settings, "codex_home"); C<TextBox>("Extension").Text = Str(settings, "extension"); C<TextBox>("Caller").Text = Str(settings, "caller"); C<TextBox>("LocalIP").Text = Str(settings, "advertise_ip"); C<TextBox>("SipPort").Text = Str(settings, "sip_port", "5060");
                selectedIds.Clear(); foreach (object x in Items(settings["selected_threads"])) selectedIds.Add(Convert.ToString(x));
                PaintSwitch(); EngineChanged(); SoundLabels(); initializing = false; loaded = true;
            } else if (!changingSwitch && C<CheckBox>("EnabledSwitch").IsChecked != Bool(settings, "enabled")) { initializing = true; C<CheckBox>("EnabledSwitch").IsChecked = Bool(settings, "enabled"); initializing = false; PaintSwitch(); }
            var phone = Map(lastState["phone"]); var regs = phone.ContainsKey("registrations") ? Map(phone["registrations"]) : new Dictionary<string, object>();
            bool online = regs.ContainsKey(Str(settings, "extension"));
            C<TextBlock>("PhoneStatus").Text = online ? "话机已连接" : "话机未连接";
            C<TextBlock>("PhoneStatus").Foreground = Brush(online ? "#419267" : "#AB8A43"); C<Border>("PhoneBadge").Background = Brush(online ? "#E9F6EE" : "#FAF1DC"); C<System.Windows.Shapes.Ellipse>("PhoneDot").Fill = Brush(online ? "#34B775" : "#C6A659");
            C<Border>("PhoneBadge").ToolTip = Str(lastState, "bridge_error") != "" ? Str(lastState, "bridge_error") : online ? "分机 " + Str(settings, "extension") : "请检查话机账号和局域网设置";
            C<TextBlock>("QueueStatus").Text = "等待 " + Str(lastState, "pending") + " · 通话中 " + Str(lastState, "active");
            C<TextBlock>("MonitorStatus").Text = Str(lastState, "monitor_error") != "" ? Str(lastState, "monitor_error") : Str(lastState, "last_event") == "" ? "正在监听本机 Codex，等待新的完成事件。" : "最近完成事件：" + Str(lastState, "last_event");
            FillVoices(); FillHistory();
        } catch (Exception ex) { C<TextBlock>("PhoneStatus").Text = "连接异常"; Notice(ex.Message); Trace(ex.ToString()); } finally { polling = false; }
    }
    public class VoiceItem { public string Id, Label; public override string ToString() { return Label; } }
    public class TaskRow { public string Id { get; set; } public string Title { get; set; } public bool Selected { get; set; } }
    public class HistoryRow { public string Time { get; set; } public string Date { get; set; } public string Source { get; set; } public string Detail { get; set; } public string Status { get; set; } public Brush Color { get; set; } public Brush Background { get; set; } }
    void FillVoices() {
        if (lastState == null) return; var vs = Map(lastState["voices"]); string sig = Edge + ":" + json.Serialize(vs); if (sig == voiceSignature) return; voiceSignature = sig;
        string current = VoiceName(); bool wasInitializing = initializing; initializing = true;
        ComboBox box = C<ComboBox>("Voices"); box.Items.Clear(); box.Items.Add(new VoiceItem { Id = "", Label = "使用默认音色" });
        if (!Edge) foreach (object x in Items(vs["sapi"])) box.Items.Add(new VoiceItem { Id = Convert.ToString(x), Label = Convert.ToString(x).Replace("Microsoft ", "").Replace(" Desktop", "") + " · 本地" });
        else {
            var chinese = new Dictionary<string, string> { { "Yunxi", "云希" }, { "Yunyang", "云扬" }, { "Yunjian", "云健" }, { "Yunxia", "云夏" }, { "Xiaoxiao", "晓晓" }, { "Xiaoyi", "晓伊" } };
            foreach (object x in Items(vs["edge"])) { var v = Map(x); string name = Str(v, "name"), label = name; foreach (var pair in chinese) if (name == "zh-CN-" + pair.Key + "Neural") label = pair.Value + " · 普通话";
                box.Items.Add(new VoiceItem { Id = name, Label = label + " · " + (Str(v, "gender") == "Male" ? "男声" : Str(v, "gender") == "Female" ? "女声" : Str(v, "gender")) }); }
        }
        box.Text = current; foreach (VoiceItem item in box.Items) if (item.Id == current) { box.SelectedItem = item; break; }
        C<TextBlock>("VoiceHint").Text = Bool(vs, "loading") ? "正在获取音色列表…" : Edge && Str(vs, "error") != "" ? Str(vs, "error") : Edge ? "新文案需要联网合成。中文音色优先展示，也可手填音色名。" : "无需联网，使用这台电脑已安装的声音。";
        initializing = wasInitializing;
    }
    void FillHistory() {
        string sig = json.Serialize(lastState["history"]); if (sig == historySignature) return; historySignature = sig;
        var rows = new List<HistoryRow>(); var labels = new Dictionary<string, string> { { "queued", "等待拨号" }, { "calling", "正在拨号" }, { "done", "播报完成" }, { "skipped", "已跳过" }, { "failed", "失败" }, { "cancelled", "已取消" } };
        foreach (object x in Items(lastState["history"])) { var h = Map(x); string s = Str(h, "status"), at = Str(h, "at"), source = Str(h, "source"); rows.Add(new HistoryRow {
            Time = at.Length >= 19 ? at.Substring(11, 8) : at, Date = at.Length >= 10 ? at.Substring(0, 10) : "",
            Source = source == "codex" ? "Codex 自动通知" : source == "workbuddy" ? "WorkBuddy 主动调用" : "手动测试",
            Detail = Str(h, "detail", ""), Status = labels.ContainsKey(s) ? labels[s] : s,
            Color = Brush(s == "failed" ? "#C56565" : s == "done" ? "#43946B" : "#8B94A3"), Background = Brush(s == "failed" ? "#FFF0F0" : s == "done" ? "#EAF6EF" : "#F1F3F7") }); }
        C<ItemsControl>("History").ItemsSource = rows; C<TextBlock>("HistoryEmpty").Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }
    async Task RefreshTasks() {
        if (taskRows.Count > 0) selectedIds = CheckedIds();
        object rows = await Api("/threads"); taskRows = new List<TaskRow>();
        foreach (object x in Items(rows)) { var r = Map(x); taskRows.Add(new TaskRow { Id = Str(r, "id"), Title = Str(r, "title"), Selected = selectedIds.Contains(Str(r, "id")) }); }
        C<ListBox>("Tasks").ItemsSource = taskRows;
    }
    string WorkBuddyPrompt() {
        return "请记住：每次完成工作并给出最终回复前，运行下列程序请求电话汇报：\r\n\"" + Path.Combine(root, "PhoneService.exe") + "\" --call --source workbuddy\r\n程序需要先运行 AgentCall.exe，且总开关和 WorkBuddy 开关已开启。SKIPPED 表示用户关闭通知，不要擅自打开。QUEUED 只表示已入队，实际结果看通知记录。不要重复调用，不要覆盖 Codex 配置。";
    }
    void Error(Exception ex) { Trace(ex.ToString()); Notice(ex.Message); if (!smoke) MessageBox.Show(window, ex.Message, "Agent Call", MessageBoxButton.OK, MessageBoxImage.Information); }
    async Task Quit() { if (quitting) return; quitting = true; timer.Stop(); tray.Visible = false; try { await Api("/shutdown", new Dictionary<string, object>()); } catch { } window.Close(); }
    void Capture(string path) {
        window.UpdateLayout(); var visual = C<Border>("Shell");
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(visual.ActualWidth), (int)Math.Ceiling(visual.ActualHeight), 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(visual); var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); using (var stream = File.Create(path)) encoder.Save(stream);
    }
    async Task SmokeTest() {
        await Task.Delay(1800); await Poll();
        C<RadioButton>("BackendEdge").IsChecked = true; C<ComboBox>("Voices").Text = "zh-CN-YunxiNeural";
        C<Slider>("Rate").Value = 25; C<Slider>("Pitch").Value = 12; C<Slider>("Volume").Value = -15;
        var payload = Collect();
        if (Num(payload, "rate") != 25 || Num(payload, "pitch") != 12 || Num(payload, "volume") != -15 || C<TextBlock>("VolumeValue").Text != "85%") throw new Exception("Slider mapping failed");
        await SaveAll(); var s = Map(Map(await Api("/state"))["settings"]);
        if (Num(s, "rate") != 25 || Num(s, "pitch") != 12 || Num(s, "volume") != -15 || Str(s,"voice") != "zh-CN-YunxiNeural") throw new Exception("Slider or voice save/readback failed");
        foreach (int index in new[] { 0, 1, 2, 3 }) { C<RadioButton>(new[] { "NavVoice", "NavTasks", "NavConnect", "NavHistory" }[index]).IsChecked = true; await Task.Delay(100); Capture(Path.Combine(data, "desktop-" + index + ".png")); }
        C<Button>("ResetSound").RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        if (C<Slider>("Rate").Value != 0 || C<Slider>("Pitch").Value != 0 || C<Slider>("Volume").Value != 0) throw new Exception("Reset failed");
        C<RadioButton>("BackendSapi").IsChecked = true;
        if (C<Slider>("Pitch").IsEnabled || C<Slider>("Volume").IsEnabled) throw new Exception("Local voice controls not disabled");
        File.WriteAllText(Path.Combine(data, "desktop-smoke.txt"), "PASS: window, four pages, slider live labels, value mapping, settings readback, voice selection, reset and backend-specific disabling.");
        await Quit();
    }
    [STAThread] static void Main(string[] args) {
        bool smoke = Array.IndexOf(args, "--smoke-ui") >= 0;
        bool fresh; using (var mutex = new Mutex(true, smoke ? "Local\\CodexPhoneDesktopSmoke" : "Local\\CodexPhoneDesktop", out fresh)) {
            if (!fresh) { MessageBox.Show("程序已在运行，请从右下角托盘打开。", "Agent Call"); return; }
            var application = new Application(); application.ShutdownMode = ShutdownMode.OnMainWindowClose;
            application.DispatcherUnhandledException += delegate(object sender, DispatcherUnhandledExceptionEventArgs e) { Trace(e.Exception.ToString()); e.Handled = true; MessageBox.Show(e.Exception.Message, "Agent Call"); };
            try { var desktop = new Desktop(smoke, Array.IndexOf(args, "--capture-ui") >= 0); application.Run(desktop.window); }
            catch (Exception ex) { Trace(ex.ToString()); if (!smoke) MessageBox.Show(ex.Message, "Agent Call"); Environment.ExitCode = 1; }
        }
    }
}
