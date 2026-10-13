// whsport GUI —— EUI-NEO 前端，逻辑仍走 Python 引擎（JSON-RPC）。
// 只在用户点「提交」时才真正发服务器；「预览」纯本地。

#include "eui_neo.h"

#include "engine_client.h"
#include "uda_bridge.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>

namespace app {

namespace {

constexpr float kTrackW = 620.0f;
constexpr float kTrackH = 170.0f;

struct State {
    int tab = 0;
    std::string user;
    std::string pass;
    std::string dist = "2000";
    std::string time = "830";
    std::string steps = "1100";
    std::string weight = "62";
    std::string lat = "39.9042";
    std::string lon = "116.4074";
    bool jitter = true;
    bool policy = true;
    bool busy = false;
    std::string status = "就绪";
    std::string cred = "读取中...";
    std::string metrics;
    std::vector<float> pace;
    std::string routeSvg;
    EngineClient engine;
    bool started = false;
    bool credLoaded = false;
};

State& st() {
    static State instance;
    return instance;
}

// 首次 compose 时按需启动引擎：优先冻结程序，其次 python -m whsport rpc。
void ensureEngine() {
    State& s = st();
    if (s.started) return;
    s.started = true;

    const char* env = std::getenv("WHS_ENGINE");
    const char* py = std::getenv("WHS_PYTHON");
    if (env && *env) {
        s.engine.start(env, {"rpc"});
    } else if (py && *py) {
        s.engine.start(py, {"-m", "whsport", "rpc"});
    } else {
        s.engine.start("whsport-engine", {"rpc"});
        if (!s.engine.running()) s.engine.start("python3", {"-m", "whsport", "rpc"});
    }
}

// 后台执行，回到主线程再用 requestUpdate() 触发重绘。
void runAsync(std::function<void()> work) {
    std::thread([work] {
        try {
            work();
        } catch (const std::exception& exc) {
            st().status = exc.what();
            uda_bridge::notify("出错了", exc.what());
        }
        st().busy = false;
        app::requestUpdate();
    }).detach();
}

void applyCredentials() {
    try {
        auto data = st().engine.request("credentials");
        std::string uid = data.value("uid", std::string("-"));
        st().cred = "uid=" + uid +
                    (data.value("has_token", false) ? " · 已登录" : " · 未登录");
    } catch (const std::exception&) {
        st().cred = "引擎未就绪";
    }
}

std::string themeName() { return uda_bridge::theme(); }

double num(const std::string& text, double fallback) {
    try {
        return std::stod(text);
    } catch (const std::exception&) {
        return fallback;
    }
}

nlohmann::json runParams() {
    State& s = st();
    return {
        {"distance", num(s.dist, 2000)}, {"duration", (int)num(s.time, 830)},
        {"steps", (int)num(s.steps, 1100)}, {"weight", num(s.weight, 62)},
        {"lat", num(s.lat, 39.9042)}, {"lon", num(s.lon, 116.4074)},
        {"jitter", s.jitter}, {"policy", s.policy},
    };
}

// 把 RPC 返回的配速序列（分/公里）存下来给线图用。
void storePace(const nlohmann::json& series) {
    std::vector<float> out;
    if (series.is_array()) {
        out.reserve(series.size());
        for (const auto& v : series) {
            if (v.is_number()) out.push_back(v.get<float>());
        }
    }
    st().pace = std::move(out);
}

// 把轨迹画成内联 SVG 折线（EUI-NEO 没有折线图元，Polygon 只能填充）。
std::string buildRouteSvg(const nlohmann::json& points) {
    if (!points.is_array() || points.size() < 2) return {};
    double minLat = 1e9, maxLat = -1e9, minLon = 1e9, maxLon = -1e9;
    for (const auto& p : points) {
        if (!p.is_array() || p.size() < 2) continue;
        const double la = p[0].get<double>(), lo = p[1].get<double>();
        minLat = std::min(minLat, la); maxLat = std::max(maxLat, la);
        minLon = std::min(minLon, lo); maxLon = std::max(maxLon, lo);
    }
    const double spanLat = std::max(maxLat - minLat, 1e-9);
    const double spanLon = std::max(maxLon - minLon, 1e-9);
    const float pad = 12.0f;
    const float sx = (kTrackW - 2 * pad) / static_cast<float>(spanLon);
    const float sy = (kTrackH - 2 * pad) / static_cast<float>(spanLat);

    std::string pts;
    std::string first;
    char buf[64];
    for (const auto& p : points) {
        if (!p.is_array() || p.size() < 2) continue;
        const float x = static_cast<float>((p[1].get<double>() - minLon) * sx) + pad;
        const float y = kTrackH - (static_cast<float>((p[0].get<double>() - minLat) * sy) + pad);
        std::snprintf(buf, sizeof(buf), "%.1f,%.1f ", x, y);
        if (pts.empty()) first = std::string(buf, std::strlen(buf));
        pts += buf;
    }
    std::string svg =
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 " +
        std::to_string(static_cast<int>(kTrackW)) + " " +
        std::to_string(static_cast<int>(kTrackH)) + "\">";
    svg += "<rect width=\"100%\" height=\"100%\" fill=\"#1b1f24\"/>";
    svg += "<polyline points=\"" + pts +
           "\" fill=\"none\" stroke=\"#3cb371\" stroke-width=\"2.5\" "
           "stroke-linejoin=\"round\" stroke-linecap=\"round\"/>\n";
    svg += "<circle cx=\"" + first.substr(0, first.find(',')) + "\" cy=\"" +
           first.substr(first.find(',') + 1) + "\" r=\"4\" fill=\"#e08a2b\"/>\n";
    svg += "</svg>";
    return svg;
}

void doPreview() {
    st().busy = true;
    st().status = "预览生成中（本地，不联网）...";
    runAsync([] {
        auto data = st().engine.request("run.preview", runParams());
        auto m = data.value("metrics", nlohmann::json::object());
        char buf[320];
        std::snprintf(buf, sizeof(buf),
                      "距离 %.0f m    用时 %.0f s    步数 %.0f    步频 %.0f\n"
                      "配速 %.2f~%.2f 分/公里    消耗 %.0f 千卡    功率 %.0f W    爬升 %.0f m",
                      m.value("distance", 0.0), m.value("time", 0.0),
                      m.value("steps", 0.0), m.value("avg_step_freq", 0.0),
                      m.value("pace_min", 0.0), m.value("pace_max", 0.0),
                      m.value("calorie", 0.0), m.value("power", 0.0),
                      m.value("ascent", 0.0));
        st().metrics = buf;
        storePace(data.value("pace", nlohmann::json::array()));
        st().routeSvg = buildRouteSvg(data.value("track", nlohmann::json::array()));
        st().status = "预览完成（未上传）";
        uda_bridge::notify("预览完成", "轨迹已在本地生成，未上传");
    });
}

void doSubmit() {
    st().busy = true;
    st().status = "提交中...";
    runAsync([] {
        auto data = st().engine.request("run.submit", runParams());
        st().status = "提交返回: " + data.value("result", nlohmann::json::object()).dump();
        uda_bridge::notify("提交结果", st().status.substr(0, 120));
    });
}

void doLogin() {
    st().busy = true;
    st().status = "登录中...";
    runAsync([] {
        nlohmann::json params = {{"username", st().user}, {"password", st().pass}};
        auto data = st().engine.request("login", params);
        st().status = "登录成功: uid=" + data.value("uid", std::string("-"));
        applyCredentials();
    });
}

void doQuery(const std::string& cmd) {
    st().busy = true;
    st().status = "查询 " + cmd + " ...";
    runAsync([cmd] {
        auto data = st().engine.request(cmd, nlohmann::json::object());
        st().metrics = data.dump(2);
        st().pace.clear();
        st().routeSvg.clear();
        st().status = "查询完成: " + cmd;
    });
}

// 标签 + 输入框。EUI-NEO 的占位符在有值时不可见，所以显式带一个标签。
void field(eui::Ui& ui, const std::string& id, const std::string& label,
           const std::string& value, float width,
           std::function<void(const std::string&)> on_change) {
    ui.column(id).gap(5.0f).width(width).content([&] {
        ui.text(id + ".label").text(label).fontSize(12.5f).opacity(0.6f).build();
        components::input(ui, id + ".input")
            .value(value)
            .placeholder("")
            .size(width, 40.0f)
            .onChange(std::move(on_change))
            .build();
    }).build();
}

// 密码框：v0.5.9 的 input 没有密码模式，这里用「长度差分」在末尾增删，
// 显示始终是等长星号（典型逐字输入/退格场景够用；整段粘贴会退化为逐字）。
void passwordField(eui::Ui& ui, const std::string& id, const std::string& label,
                   const std::string& secret, float width) {
    std::string shown(secret.size(), '*');
    field(ui, id, label, shown, width, [](const std::string& v) {
        std::string& real = st().pass;
        if (v.size() >= real.size()) real += v.substr(real.size());
        else real = real.substr(0, v.size());
    });
}

}  // namespace

const DslAppConfig& dslAppConfig() {
    static const DslAppConfig config = DslAppConfig{}
        .title("运动世界校园 · 工具")
        .pageId("whsport")
        .windowSize(920, 800)
        .textFont("YouSheBiaoTiHei-2.ttf");
    return config;
}

void compose(eui::Ui& ui, const eui::Screen& screen) {
    ensureEngine();
    State& s = st();

    if (!s.credLoaded) {
        s.credLoaded = true;
        runAsync([] { applyCredentials(); });
    }

    ui.column("root")
        .size(screen.width, screen.height)
        .padding(26.0f)
        .gap(16.0f)
        .content([&] {
            // ── 页头 ────────────────────────────────────────────────
            ui.row("header").gap(12.0f).content([&] {
                ui.column("brand").gap(4.0f).content([&] {
                    ui.text("title").text("运动世界校园").fontSize(24.0f).build();
                    ui.text("subtitle")
                        .text(s.cred + "   ·   先预览，确认后再提交")
                        .fontSize(12.5f).opacity(0.6f).build();
                }).build();
            }).build();

            components::tabs(ui, "tabs")
                .items({"登录", "跑步", "查询"})
                .selected(s.tab)
                .onChange([](int index) { st().tab = index; })
                .build();

            // ── 表单卡片 ────────────────────────────────────────────
            components::card(ui, "form_card")
                .padding(20.0f)
                .width(screen.width - 52.0f)
                .wrapContentHeight()
                .content([&] {
            if (s.tab == 0) {
                ui.column("login_form").gap(14.0f).content([&] {
                    field(ui, "user", "账号", s.user, 340.0f,
                          [](const std::string& v) { st().user = v; });
                    passwordField(ui, "pass", "密码", s.pass, 340.0f);
                    components::button(ui, "login").text("登录并保存")
                        .disabled(s.busy).onClick(doLogin).build();
                }).build();
            } else if (s.tab == 1) {
                ui.column("run_form").gap(14.0f).content([&] {
                    ui.row("params").gap(12.0f).content([&] {
                        field(ui, "dist", "距离(米)", s.dist, 150.0f,
                              [](const std::string& v) { st().dist = v; });
                        field(ui, "time", "用时(秒)", s.time, 150.0f,
                              [](const std::string& v) { st().time = v; });
                        field(ui, "steps", "步数", s.steps, 150.0f,
                              [](const std::string& v) { st().steps = v; });
                        field(ui, "weight", "体重(kg)", s.weight, 150.0f,
                              [](const std::string& v) { st().weight = v; });
                    }).build();
                    ui.row("geo").gap(12.0f).content([&] {
                        field(ui, "lat", "纬度", s.lat, 220.0f,
                              [](const std::string& v) { st().lat = v; });
                        field(ui, "lon", "经度", s.lon, 220.0f,
                              [](const std::string& v) { st().lon = v; });
                    }).build();
                    ui.row("opts").gap(28.0f).content([&] {
                        components::checkbox(ui, "jitter").text("小幅随机")
                            .checked(s.jitter)
                            .onChange([](bool v) { st().jitter = v; }).build();
                        components::checkbox(ui, "policy").text("policy=1")
                            .checked(s.policy)
                            .onChange([](bool v) { st().policy = v; }).build();
                    }).build();
                    ui.row("run_actions").gap(12.0f).content([&] {
                        components::button(ui, "preview").text("① 预览（本地 dry-run）")
                            .disabled(s.busy).onClick(doPreview).build();
                        components::button(ui, "submit").text("② 提交到服务器")
                            .disabled(s.busy).onClick(doSubmit).build();
                    }).build();
                }).build();
            } else {
                ui.row("query_actions").gap(12.0f).content([&] {
                    components::button(ui, "q_score").text("学期成绩")
                        .onClick([] { doQuery("score"); }).build();
                    components::button(ui, "q_policy").text("跑步规则")
                        .onClick([] { doQuery("policy"); }).build();
                    components::button(ui, "q_history").text("历史记录")
                        .onClick([] { doQuery("history"); }).build();
                    components::button(ui, "q_rank").text("排行榜")
                        .onClick([] { doQuery("rank"); }).build();
                }).build();
            }
                })
                .build();

            // ── 结果卡片（轨迹 + 指标）──────────────────────────────
            if (!s.pace.empty() || !s.metrics.empty()) {
                components::card(ui, "result_card")
                    .padding(20.0f)
                    .width(screen.width - 52.0f)
                    .wrapContentHeight()
                    .content([&] {
                        ui.column("result").gap(12.0f).content([&] {
                            if (!s.metrics.empty()) {
                                ui.text("metrics_text").text(s.metrics)
                                    .fontSize(14.0f).build();
                            }
                            if (!s.routeSvg.empty()) {
                                ui.svg("route")
                                    .source(s.routeSvg)
                                    .size(kTrackW, kTrackH)
                                    .build();
                            }
                        }).build();
                    })
                    .build();
            }

            // 状态不用应用内 toast（会被布局挤位）：底部一行文字 + UDA 系统通知
            // （重要事件在 doPreview/doSubmit/runAsync 里调 uda_bridge::notify）。
            ui.text("status").text(s.status).fontSize(12.5f).opacity(0.55f).build();
        })
        .build();
}

}  // namespace app
