#include "websocket_protocol.h"
#include "application.h"
#include "board.h"
#include "settings.h"
#include "system_info.h"

#include <esp_log.h>
#include <arpa/inet.h>
#include <cJSON.h>
#include <cstring>
#include "assets/lang_config.h"

#if CONFIG_ENGLISH_MVP_CLOUD
#include <esp_netif_sntp.h>
#include <nvs_flash.h>
#include <ctime>
#endif

#define TAG "WS"

#if CONFIG_ENGLISH_MVP_CLOUD
namespace {
bool LoadCloudConnection(std::string& url, std::string& token) {
    // This partition is provisioned separately: never erase Wi-Fi/default NVS.
    static bool initialized = false;
    if (!initialized) {
        if (nvs_flash_init_partition("cloudcfg") != ESP_OK) {
            ESP_LOGE(TAG, "Cloud credential partition unavailable");
            return false;
        }
        initialized = true;
    }
    nvs_handle_t handle = 0;
    if (nvs_open_from_partition("cloudcfg", "websocket", NVS_READONLY, &handle) != ESP_OK) {
        ESP_LOGE(TAG, "Cloud credentials not provisioned");
        return false;
    }
    auto read = [handle](const char* key, std::string& value) {
        size_t length = 0;
        if (nvs_get_str(handle, key, nullptr, &length) != ESP_OK || length < 2 || length > 257) {
            return false;
        }
        value.resize(length);
        if (nvs_get_str(handle, key, value.data(), &length) != ESP_OK)
            return false;
        value.resize(length - 1);
        return value.find('\0') == std::string::npos;
    };
    std::string device;
    bool ok = read("url", url) && read("token", token) && read("device_id", device);
    nvs_close(handle);
    // Restrict credential disclosure to our fixed product endpoint, not arbitrary NVS URLs.
    ok = ok && url == "wss://english.example.com/xiaozhi/v1/" &&
         device == SystemInfo::GetMacAddress() && token.size() >= 32 &&
         token.find_first_not_of(
             "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") ==
             std::string::npos;
    if (!ok) {
        token.clear();
        ESP_LOGE(TAG, "Invalid cloud configuration or device binding");
        return false;
    }
    return true;
}

bool EnsureCloudClock() {
    // OpenAudioChannel runs in the existing connection worker, never the audio/main task.
    static bool started = false;
    if (!started) {
        esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG("ntp.aliyun.com");
        if (esp_netif_sntp_init(&config) != ESP_OK)
            return false;
        started = true;
    }
    if (std::time(nullptr) < 1704067200) {
        if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000)) != ESP_OK) {
            ESP_LOGW(TAG, "Cloud clock not synchronized; retry without bypassing TLS");
            return false;
        }
    }
    return std::time(nullptr) >= 1704067200;
}
}  // namespace
#endif

WebsocketProtocol::WebsocketProtocol() { event_group_handle_ = xEventGroupCreate(); }

WebsocketProtocol::~WebsocketProtocol() { vEventGroupDelete(event_group_handle_); }

bool WebsocketProtocol::Start() {
    // Only connect to server when audio channel is needed
    return true;
}

bool WebsocketProtocol::SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
    if (websocket_ == nullptr || !websocket_->IsConnected()) {
        return false;
    }

    if (version_ == 2) {
        std::string serialized;
        serialized.resize(sizeof(BinaryProtocol2) + packet->payload.size());
        auto bp2 = (BinaryProtocol2*)serialized.data();
        bp2->version = htons(version_);
        bp2->type = 0;
        bp2->reserved = 0;
        bp2->timestamp = htonl(packet->timestamp);
        bp2->payload_size = htonl(packet->payload.size());
        memcpy(bp2->payload, packet->payload.data(), packet->payload.size());

        return websocket_->Send(serialized.data(), serialized.size(), true);
    } else if (version_ == 3) {
        std::string serialized;
        serialized.resize(sizeof(BinaryProtocol3) + packet->payload.size());
        auto bp3 = (BinaryProtocol3*)serialized.data();
        bp3->type = 0;
        bp3->reserved = 0;
        bp3->payload_size = htons(packet->payload.size());
        memcpy(bp3->payload, packet->payload.data(), packet->payload.size());

        return websocket_->Send(serialized.data(), serialized.size(), true);
    } else {
        return websocket_->Send(packet->payload.data(), packet->payload.size(), true);
    }
}

bool WebsocketProtocol::SendText(const std::string& text) {
    if (websocket_ == nullptr || !websocket_->IsConnected()) {
        return false;
    }

    if (!websocket_->Send(text)) {
        ESP_LOGE(TAG, "Failed to send text: %s", text.c_str());
        SetError(Lang::Strings::SERVER_ERROR);
        return false;
    }

    return true;
}

bool WebsocketProtocol::IsAudioChannelOpened() const {
    return websocket_ != nullptr && websocket_->IsConnected() && !error_occurred_ && !IsTimeout();
}

void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {
    (void)send_goodbye;  // Websocket doesn't need to send goodbye message
    websocket_.reset();
}

bool WebsocketProtocol::OpenAudioChannel() {
    Settings settings("websocket", false);
    std::string url = settings.GetString("url");
    std::string token = settings.GetString("token");
    int version = settings.GetInt("version");
    if (version != 0) {
        version_ = version;
    }

#if CONFIG_ENGLISH_MVP_LOCAL
    mvp_request_id_.clear();
#if CONFIG_ENGLISH_MVP_CLOUD
    if (!LoadCloudConnection(url, token) || !EnsureCloudClock())
        return false;
#else
    url = CONFIG_ENGLISH_MVP_WS_URL;
    token.clear();
#endif
    version_ = 1;
    if (url.empty()) {
        ESP_LOGE(TAG, "Set ENGLISH_MVP_WS_URL to the Mac LAN endpoint before building");
        return false;
    }
#endif
    error_occurred_ = false;
    xEventGroupClearBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);

    auto network = Board::GetInstance().GetNetwork();
    websocket_ = network->CreateWebSocket(1);
    if (websocket_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create websocket");
        return false;
    }

    if (!token.empty()) {
        // If token not has a space, add "Bearer " prefix
        if (token.find(" ") == std::string::npos) {
            token = "Bearer " + token;
        }
        websocket_->SetHeader("Authorization", token.c_str());
    }
    websocket_->SetHeader("Protocol-Version", std::to_string(version_).c_str());
    websocket_->SetHeader("Device-Id", SystemInfo::GetMacAddress().c_str());
    websocket_->SetHeader("Client-Id", Board::GetInstance().GetUuid().c_str());

    websocket_->OnData([this](const char* data, size_t len, bool binary) {
        if (binary) {
            if (on_incoming_audio_ != nullptr) {
                if (version_ == 2) {
                    BinaryProtocol2* bp2 = (BinaryProtocol2*)data;
                    bp2->version = ntohs(bp2->version);
                    bp2->type = ntohs(bp2->type);
                    bp2->timestamp = ntohl(bp2->timestamp);
                    bp2->payload_size = ntohl(bp2->payload_size);
                    auto payload = (uint8_t*)bp2->payload;
                    on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
                        .sample_rate = server_sample_rate_,
                        .frame_duration = server_frame_duration_,
                        .timestamp = bp2->timestamp,
                        .payload = std::vector<uint8_t>(payload, payload + bp2->payload_size)}));
                } else if (version_ == 3) {
                    BinaryProtocol3* bp3 = (BinaryProtocol3*)data;
                    bp3->type = bp3->type;
                    bp3->payload_size = ntohs(bp3->payload_size);
                    auto payload = (uint8_t*)bp3->payload;
                    on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
                        .sample_rate = server_sample_rate_,
                        .frame_duration = server_frame_duration_,
                        .timestamp = 0,
                        .payload = std::vector<uint8_t>(payload, payload + bp3->payload_size)}));
                } else {
                    on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
                        .sample_rate = server_sample_rate_,
                        .frame_duration = server_frame_duration_,
                        .timestamp = 0,
                        .payload = std::vector<uint8_t>((uint8_t*)data, (uint8_t*)data + len),
                        .mvp_request_id = mvp_request_id_}));
                }
            }
        } else {
            // Parse JSON data
            auto root = cJSON_ParseWithLength(data, len);
            auto type = cJSON_GetObjectItem(root, "type");
#if CONFIG_ENGLISH_MVP_LOCAL
            auto request_id = cJSON_GetObjectItem(root, "request_id");
            if (cJSON_IsString(request_id)) {
                const std::string value = request_id->valuestring;
                if (value.size() == 32 &&
                    value.find_first_not_of("0123456789abcdef") == std::string::npos) {
                    mvp_request_id_ = value;
                }
            }
#endif
            if (cJSON_IsString(type)) {
                if (strcmp(type->valuestring, "hello") == 0) {
                    ParseServerHello(root);
                } else {
                    if (on_incoming_json_ != nullptr) {
                        on_incoming_json_(root);
                    }
                }
            } else {
                ESP_LOGE(TAG, "Missing message type, data: %s", std::string(data, len).c_str());
            }
            cJSON_Delete(root);
        }
        last_incoming_time_ = std::chrono::steady_clock::now();
    });

    websocket_->OnDisconnected([this]() {
        ESP_LOGI(TAG, "Websocket disconnected");
        if (on_audio_channel_closed_ != nullptr) {
            on_audio_channel_closed_();
        }
    });

    ESP_LOGI(TAG, "Connecting to websocket server: %s with version: %d", url.c_str(), version_);
    if (auto connected = websocket_->Connect(url.c_str()); !connected) {
        ESP_LOGE(TAG, "Failed to connect to websocket server: %s",
                 connected.error().ToString().c_str());
        SetError(Lang::Strings::SERVER_NOT_CONNECTED, url);
        return false;
    }

    // Send hello message to describe the client
    auto message = GetHelloMessage();
    if (!SendText(message)) {
        return false;
    }

    // Wait for server hello
    EventBits_t bits =
        xEventGroupWaitBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT, pdTRUE,
                            pdFALSE, pdMS_TO_TICKS(10000));
    if (!(bits & WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT)) {
        ESP_LOGE(TAG, "Failed to receive server hello");
        SetError(Lang::Strings::SERVER_TIMEOUT);
        return false;
    }

    if (on_audio_channel_opened_ != nullptr) {
        on_audio_channel_opened_();
    }

    return true;
}

std::string WebsocketProtocol::GetHelloMessage() {
    // keys: message type, version, audio_params (format, sample_rate, channels)
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "hello");
    cJSON_AddNumberToObject(root, "version", version_);
    cJSON* features = cJSON_CreateObject();
#if CONFIG_USE_SERVER_AEC
    cJSON_AddBoolToObject(features, "aec", true);
#endif
#if !CONFIG_ENGLISH_MVP_LOCAL
    cJSON_AddBoolToObject(features, "mcp", true);
#else
    cJSON_AddBoolToObject(features, "mvp_playback_completed", true);
    cJSON_AddBoolToObject(features, "mvp_teaching_mode", true);
#if CONFIG_ENGLISH_MVP_CLOUD
    cJSON_AddBoolToObject(features, "mvp_device_controls", true);
#endif
#if CONFIG_ENGLISH_MVP_DUPLEX
    cJSON_AddBoolToObject(features, "mvp_duplex", true);
    cJSON_AddBoolToObject(features, "mvp_aec_reference", true);
#endif
#endif
    cJSON_AddItemToObject(root, "features", features);
    AddTextFontCapabilities(root);
    cJSON_AddStringToObject(root, "transport", "websocket");
    cJSON* audio_params = cJSON_CreateObject();
    cJSON_AddStringToObject(audio_params, "format", "opus");
    cJSON_AddNumberToObject(audio_params, "sample_rate", 16000);
    cJSON_AddNumberToObject(audio_params, "channels", 1);
    cJSON_AddNumberToObject(audio_params, "frame_duration", OPUS_FRAME_DURATION_MS);
    cJSON_AddItemToObject(root, "audio_params", audio_params);
    auto json_str = cJSON_PrintUnformatted(root);
    std::string message(json_str);
    cJSON_free(json_str);
    cJSON_Delete(root);
    return message;
}

void WebsocketProtocol::ParseServerHello(const cJSON* root) {
    auto transport = cJSON_GetObjectItem(root, "transport");
    if (!cJSON_IsString(transport)) {
        ESP_LOGE(TAG, "Missing or non-string transport in server hello");
        return;
    }
    if (strcmp(transport->valuestring, "websocket") != 0) {
        ESP_LOGE(TAG, "Unsupported transport: %s", transport->valuestring);
        return;
    }

    auto session_id = cJSON_GetObjectItem(root, "session_id");
    if (cJSON_IsString(session_id)) {
        session_id_ = session_id->valuestring;
        ESP_LOGI(TAG, "Session ID: %s", session_id_.c_str());
    }

    auto audio_params = cJSON_GetObjectItem(root, "audio_params");
    if (cJSON_IsObject(audio_params)) {
        auto sample_rate = cJSON_GetObjectItem(audio_params, "sample_rate");
        if (cJSON_IsNumber(sample_rate)) {
            server_sample_rate_ = sample_rate->valueint;
        }
        auto frame_duration = cJSON_GetObjectItem(audio_params, "frame_duration");
        if (cJSON_IsNumber(frame_duration)) {
            server_frame_duration_ = frame_duration->valueint;
        }
    }

    xEventGroupSetBits(event_group_handle_, WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT);
}
