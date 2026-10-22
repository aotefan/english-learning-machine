#include "application.h"
#include "assets.h"
#include "assets/lang_config.h"
#include "audio_codec.h"
#include "board.h"
#include "cjson_utils.h"
#include "display.h"
#include "mcp_server.h"
#include "mqtt_protocol.h"
#include "settings.h"
#include "system_info.h"
#include "text_glyph_payload.h"
#include "websocket_protocol.h"

#include <driver/gpio.h>
#include <esp_log.h>
#include <arpa/inet.h>
#include <cJSON.h>
#include <algorithm>
#include <cstring>
#include <limits>

#define TAG "Application"

Application::Application() : notify_player_(audio_service_) {
    event_group_ = xEventGroupCreate();

#if CONFIG_USE_DEVICE_AEC && CONFIG_USE_SERVER_AEC
#error "CONFIG_USE_DEVICE_AEC and CONFIG_USE_SERVER_AEC cannot be enabled at the same time"
#elif CONFIG_USE_DEVICE_AEC
    aec_mode_ = kAecOnDeviceSide;
#elif CONFIG_USE_SERVER_AEC
    aec_mode_ = kAecOnServerSide;
#else
    aec_mode_ = kAecOff;
#endif

    esp_timer_create_args_t clock_timer_args = {.callback =
                                                    [](void* arg) {
                                                        Application* app = (Application*)arg;
                                                        xEventGroupSetBits(app->event_group_,
                                                                           MAIN_EVENT_CLOCK_TICK);
                                                    },
                                                .arg = this,
                                                .dispatch_method = ESP_TIMER_TASK,
                                                .name = "clock_timer",
                                                .skip_unhandled_events = true};
    esp_timer_create(&clock_timer_args, &clock_timer_handle_);
}

Application::~Application() {
    notify_player_.Stop();
    if (clock_timer_handle_ != nullptr) {
        esp_timer_stop(clock_timer_handle_);
        esp_timer_delete(clock_timer_handle_);
    }
    vEventGroupDelete(event_group_);
}

bool Application::SetDeviceState(DeviceState state) { return state_machine_.TransitionTo(state); }

void Application::Initialize() {
    auto& board = Board::GetInstance();
    SetDeviceState(kDeviceStateStarting);

    // Setup the display
    auto display = board.GetDisplay();
    display->SetupUI();
    // Print board name/version info
    display->SetChatMessage("system", SystemInfo::GetUserAgent().c_str());

    // Setup the audio service
    auto codec = board.GetAudioCodec();
    audio_service_.Initialize(codec);
    audio_service_.Start();
    ESP_LOGI(TAG, "After board/audio init");
    SystemInfo::PrintHeapStats();

    AudioServiceCallbacks callbacks;
    callbacks.on_send_queue_available = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_SEND_AUDIO);
    };
    callbacks.on_wake_word_detected = [this](const std::string& wake_word) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_WAKE_WORD_DETECTED);
    };
    callbacks.on_vad_change = [this](bool speaking) {
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        Schedule([this, speaking]() { HandleDeviceVadChange(speaking); });
#endif
        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);
    };
    callbacks.on_playback_drained = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_PLAYBACK_DRAINED);
    };
    callbacks.on_playback_progress = [this](uint32_t playback_id, uint32_t media_position_ms) {
        notify_player_.OnPlaybackProgress(playback_id, media_position_ms);
    };
#if CONFIG_ENGLISH_MVP_LOCAL
    callbacks.on_local_playback_started = [this](const std::string& request_id) {
        Schedule([this, request_id]() {
            if (request_id == mvp_playback_request_id_ && protocol_ && !mvp_connecting_ &&
                protocol_->IsAudioChannelOpened()) {
                mvp_playback_started_ = protocol_->SendPlaybackStarted(request_id
#if CONFIG_ENGLISH_MVP_DUPLEX
                                                                       ,
                                                                       esp_timer_get_time()
#endif
                );
                MaybeConfirmMvpPlayback();
            }
        });
    };
#endif
    audio_service_.SetCallbacks(callbacks);

    // Add state change listeners
    state_machine_.AddStateChangeListener([this](DeviceState old_state, DeviceState new_state) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_STATE_CHANGED);
    });

    // Start the clock timer to update the status bar
    esp_timer_start_periodic(clock_timer_handle_, 1000000);

    // Add MCP common tools (only once during initialization)
#if !CONFIG_ENGLISH_MVP_LOCAL || CONFIG_ENGLISH_MVP_CLOUD
    auto& mcp_server = McpServer::GetInstance();
    mcp_server.AddCommonTools();
#if !CONFIG_ENGLISH_MVP_LOCAL
    mcp_server.AddUserOnlyTools();
#endif
#endif

    // Set network event callback for UI updates and network state handling
    board.SetNetworkEventCallback([this](NetworkEvent event, const std::string& data) {
        auto display = Board::GetInstance().GetDisplay();

        switch (event) {
            case NetworkEvent::Scanning:
                display->ShowNotification(Lang::Strings::SCANNING_WIFI, 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::Connecting: {
                if (data.empty()) {
                    // Cellular network - registering without carrier info yet
                    display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                } else {
                    // WiFi or cellular with carrier info
                    std::string msg = Lang::Strings::CONNECT_TO;
                    msg += data;
                    msg += "...";
                    display->ShowNotification(msg.c_str(), 30000);
                }
                break;
            }
            case NetworkEvent::Connected: {
                std::string msg = Lang::Strings::CONNECTED_TO;
                msg += data;
                display->ShowNotification(msg.c_str(), 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_CONNECTED);
                break;
            }
            case NetworkEvent::Disconnected:
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::WifiConfigModeEnter:
                // WiFi config mode enter is handled by WifiBoard internally
                break;
            case NetworkEvent::WifiConfigModeExit:
                // WiFi config mode exit is handled by WifiBoard internally
                break;
            // Cellular modem specific events
            case NetworkEvent::ModemDetecting:
                display->SetStatus(Lang::Strings::DETECTING_MODULE);
                break;
            case NetworkEvent::ModemErrorNoSim:
                Alert(Lang::Strings::ERROR, Lang::Strings::PIN_ERROR, "warning",
                      Lang::Sounds::OGG_ERR_PIN);
                break;
            case NetworkEvent::ModemErrorRegDenied:
                Alert(Lang::Strings::ERROR, Lang::Strings::REG_ERROR, "warning",
                      Lang::Sounds::OGG_ERR_REG);
                break;
            case NetworkEvent::ModemErrorInitFailed:
                Alert(Lang::Strings::ERROR, Lang::Strings::MODEM_INIT_ERROR, "warning",
                      Lang::Sounds::OGG_EXCLAMATION);
                break;
            case NetworkEvent::ModemErrorTimeout:
                display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                break;
        }
    });

    // Start network asynchronously
    board.StartNetwork();

    // Update the status bar immediately to show the network state
    display->UpdateStatusBar(true);
}

void Application::Run() {
    // Set the priority of the main task to 10
    vTaskPrioritySet(nullptr, 10);

    const EventBits_t ALL_EVENTS =
        MAIN_EVENT_SCHEDULE | MAIN_EVENT_SEND_AUDIO | MAIN_EVENT_WAKE_WORD_DETECTED |
        MAIN_EVENT_VAD_CHANGE | MAIN_EVENT_CLOCK_TICK | MAIN_EVENT_ERROR |
        MAIN_EVENT_NETWORK_CONNECTED | MAIN_EVENT_NETWORK_DISCONNECTED | MAIN_EVENT_TOGGLE_CHAT |
        MAIN_EVENT_START_LISTENING | MAIN_EVENT_STOP_LISTENING | MAIN_EVENT_ACTIVATION_DONE |
        MAIN_EVENT_STATE_CHANGED | MAIN_EVENT_PLAYBACK_DRAINED;

    while (true) {
        auto bits = xEventGroupWaitBits(event_group_, ALL_EVENTS, pdTRUE, pdFALSE, portMAX_DELAY);

        if (bits & MAIN_EVENT_ERROR) {
            if (GetDeviceState() == kDeviceStateNotifying) {
                StopNotification();
            }
            SetDeviceState(kDeviceStateIdle);
            Alert(Lang::Strings::ERROR, last_error_message_.c_str(), "cancel",
                  Lang::Sounds::OGG_EXCLAMATION);
        }

        if (bits & MAIN_EVENT_NETWORK_CONNECTED) {
            HandleNetworkConnectedEvent();
        }

        if (bits & MAIN_EVENT_NETWORK_DISCONNECTED) {
            HandleNetworkDisconnectedEvent();
        }

        if (bits & MAIN_EVENT_ACTIVATION_DONE) {
            HandleActivationDoneEvent();
        }

        if (bits & MAIN_EVENT_STATE_CHANGED) {
            HandleStateChangedEvent();
        }

        if (bits & MAIN_EVENT_PLAYBACK_DRAINED) {
            if (audio_service_.IsPlaybackIdle()) {
                notify_player_.OnPlaybackDrained();
#if CONFIG_ENGLISH_MVP_LOCAL
                MaybeConfirmMvpPlayback();
#endif
            }
            // Deferred listening start (auto mode): the playback queue has
            // drained, so it is now safe to enable voice processing.
            if (pending_listening_start_ && GetDeviceState() == kDeviceStateListening &&
                audio_service_.IsPlaybackIdle()) {
                pending_listening_start_ = false;
                StartListeningAudio();
            }
        }

        if (bits & MAIN_EVENT_TOGGLE_CHAT) {
            HandleToggleChatEvent();
        }

        if (bits & MAIN_EVENT_START_LISTENING) {
            HandleStartListeningEvent();
        }

        if (bits & MAIN_EVENT_STOP_LISTENING) {
            HandleStopListeningEvent();
        }

        if (bits & MAIN_EVENT_SEND_AUDIO) {
            while (auto packet = audio_service_.PopPacketFromSendQueue()) {
#if CONFIG_ENGLISH_MVP_LOCAL
                if (mvp_connecting_ || !mvp_capture_started_) {
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
                    if (mvp_vad_monitoring_ && !mvp_connecting_) {
                        mvp_vad_preroll_.push_back(std::move(packet));
                        if (mvp_vad_preroll_.size() > 34) {
                            mvp_vad_preroll_.pop_front();
                        }
                    }
#endif
                    continue;
                }
#endif
                if (protocol_ && !protocol_->SendAudio(std::move(packet))) {
                    // Drop the remaining packets. Leaving them in the queue would
                    // stall the Opus codec task (it waits for queue space), which in
                    // turn deadlocks the whole audio input pipeline, as no new
                    // MAIN_EVENT_SEND_AUDIO event would ever be triggered again.
                    while (audio_service_.PopPacketFromSendQueue())
                        ;
                    break;
                }
            }
#if CONFIG_ENGLISH_MVP_LOCAL
            FinishMvpUpload();
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
            MaybeFinishDeviceVadTurn();
            // FinishRawCapture acknowledges its stop marker through this event.
            // Playback may already have drained before that acknowledgement.
            MaybeResumeMvpAutoListening();
#endif
#endif
        }

        if (bits & MAIN_EVENT_WAKE_WORD_DETECTED) {
            HandleWakeWordDetectedEvent();
        }

        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (GetDeviceState() == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
            }
        }

        if (bits & MAIN_EVENT_SCHEDULE) {
            std::unique_lock<std::mutex> lock(mutex_);
            auto tasks = std::move(main_tasks_);
            lock.unlock();
            for (auto& task : tasks) {
                task();
            }
        }

        if (bits & MAIN_EVENT_CLOCK_TICK) {
            clock_ticks_++;
#if CONFIG_ENGLISH_MVP_LOCAL
            FinishMvpUpload();
            MvpNetworkTick();
            if ((!mvp_auto_mode_
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
                 || mvp_vad_streaming_
#endif
                 ) &&
                mvp_capture_started_ && !mvp_stopping_ && !mvp_waiting_response_ &&
                esp_timer_get_time() > mvp_deadline_us_) {
                HandleStopListeningEvent();
            }
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
            MaybeFinishDeviceVadTurn();
#endif
            if ((mvp_stopping_ || mvp_waiting_response_) &&
                esp_timer_get_time() > mvp_deadline_us_) {
                ESP_LOGE(TAG, "MVP request deadline exceeded; closing audio channel");
                mvp_stopping_ = false;
                mvp_waiting_response_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
                ResetDeviceVad();
#endif
                audio_service_.FinishRawCapture();
                protocol_->CloseAudioChannel();
                SetDeviceState(kDeviceStateIdle);
            }
#endif
            auto display = Board::GetInstance().GetDisplay();
            display->UpdateStatusBar();

            // Print debug info every 10 seconds
            if (clock_ticks_ % 10 == 0) {
                SystemInfo::PrintHeapStats();
                // SystemInfo::PrintTaskList();
                // SystemInfo::PrintTaskCpuUsage(pdMS_TO_TICKS(1000));
            }
        }
    }
}

void Application::HandleNetworkConnectedEvent() {
#if CONFIG_ENGLISH_MVP_LOCAL
    mvp_network_ready_ = true;
    mvp_retry_at_us_ = 0;
#endif
    ESP_LOGI(TAG, "Network connected");
    auto state = GetDeviceState();

    if (state == kDeviceStateStarting || state == kDeviceStateWifiConfiguring) {
        // Network is ready, start activation
        SetDeviceState(kDeviceStateActivating);
        if (activation_task_handle_ != nullptr) {
            ESP_LOGW(TAG, "Activation task already running");
            return;
        }

        xTaskCreate(
            [](void* arg) {
                Application* app = static_cast<Application*>(arg);
                app->ActivationTask();
                app->activation_task_handle_ = nullptr;
                vTaskDelete(NULL);
            },
            "activation", 4096 * 2, this, 2, &activation_task_handle_);
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleNetworkDisconnectedEvent() {
#if CONFIG_ENGLISH_MVP_LOCAL
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    ResetDeviceVad();
#endif
    mvp_network_ready_ = false;
    mvp_teaching_mode_ = "unknown";
    ++mvp_network_epoch_;
    mvp_want_recording_ = false;
    mvp_waiting_response_ = false;
    mvp_stopping_ = false;
    mvp_capture_started_ = false;
    mvp_receiving_audio_ = false;
    audio_service_.FinishRawCapture();
    if (protocol_ && !mvp_connecting_) {
        protocol_->CloseAudioChannel();
    }
    SetDeviceState(kDeviceStateIdle);
    Board::GetInstance().GetDisplay()->UpdateStatusBar(true);
    return;
#endif
    // Close current conversation when network disconnected
    auto state = GetDeviceState();
    if (state == kDeviceStateNotifying) {
        StopNotification();
    }
    if (state == kDeviceStateConnecting || state == kDeviceStateListening ||
        state == kDeviceStateSpeaking) {
        ESP_LOGI(TAG, "Closing audio channel due to network disconnection");
        protocol_->CloseAudioChannel();
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleActivationDoneEvent() {
    ESP_LOGI(TAG, "Activation done");

    SystemInfo::PrintHeapStats();
    SetDeviceState(kDeviceStateIdle);

    has_server_time_ = ota_->HasServerTime();

    // Protocol start may have already raised MAIN_EVENT_ERROR. Do not replace
    // that alert with the "ready" UI/sound — the main loop can process both
    // events back-to-back because the activation task is lower priority.
    const bool has_error = !last_error_message_.empty();
    if (!has_error) {
        auto display = Board::GetInstance().GetDisplay();
        std::string message = std::string(Lang::Strings::VERSION) + ota_->GetCurrentVersion();
        display->ShowNotification(message.c_str());
        display->SetChatMessage("system", "");
    }

    // Release OTA object after activation is complete
    ota_.reset();
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);

    if (!has_error) {
        Schedule([this]() {
            // Play the success sound to indicate the device is ready
            audio_service_.PlaySound(Lang::Sounds::OGG_SUCCESS);
        });
    }
}

void Application::ActivationTask() {
    // Create OTA object for activation process
    ota_ = std::make_unique<Ota>();

#if CONFIG_ENGLISH_MVP_LOCAL
    ota_->MarkCurrentVersionValid();
#else
    // Check for new assets version
    CheckAssetsVersion();

    // Check for new firmware version
    CheckNewVersion();
#endif

    // Initialize the protocol
    InitializeProtocol();

    // Signal completion to main loop
    xEventGroupSetBits(event_group_, MAIN_EVENT_ACTIVATION_DONE);
}

void Application::CheckAssetsVersion() {
    // Only allow CheckAssetsVersion to be called once
    if (assets_version_checked_) {
        return;
    }
    assets_version_checked_ = true;

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto& assets = Assets::GetInstance();

    if (!assets.partition_valid()) {
        ESP_LOGW(TAG, "Assets partition is disabled for board %s", BOARD_NAME);
        return;
    }

    Settings settings("assets", true);
    // Check if there is a new assets need to be downloaded
    std::string download_url = settings.GetString("download_url");

    if (!download_url.empty()) {
        settings.EraseKey("download_url");

        char message[256];
        snprintf(message, sizeof(message), Lang::Strings::FOUND_NEW_ASSETS, download_url.c_str());
        Alert(Lang::Strings::LOADING_ASSETS, message, "cloud_download", Lang::Sounds::OGG_UPGRADE);

        // Wait for the audio service to be idle for 3 seconds
        vTaskDelay(pdMS_TO_TICKS(3000));
        SetDeviceState(kDeviceStateUpgrading);
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        display->SetChatMessage("system", Lang::Strings::PLEASE_WAIT);

        bool success =
            assets.Download(download_url, [this, display](int progress, size_t speed) -> void {
                char buffer[32];
                snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
                Schedule([display, message = std::string(buffer)]() {
                    display->SetChatMessage("system", message.c_str());
                });
            });

        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        vTaskDelay(pdMS_TO_TICKS(1000));

        if (!success) {
            Alert(Lang::Strings::ERROR, Lang::Strings::DOWNLOAD_ASSETS_FAILED, "cancel",
                  Lang::Sounds::OGG_EXCLAMATION);
            vTaskDelay(pdMS_TO_TICKS(2000));
            SetDeviceState(kDeviceStateActivating);
            return;
        }
    }

    // Apply assets
    assets.Apply();
    display->SetChatMessage("system", "");
    display->SetEmotion("robot_2");
}

void Application::CheckNewVersion() {
    const int MAX_RETRY = 10;
    int retry_count = 0;
    int retry_delay = 10;  // Initial retry delay in seconds

    auto& board = Board::GetInstance();
    while (true) {
        auto display = board.GetDisplay();
        display->SetStatus(Lang::Strings::CHECKING_NEW_VERSION);

        auto check = ota_->CheckVersion();
        if (!check) {
            retry_count++;
            if (retry_count >= MAX_RETRY) {
                ESP_LOGE(TAG, "Too many retries, exit version check");
                return;
            }

            const auto& err = check.error();
            char error_message[160];
            int error_message_length =
                snprintf(error_message, sizeof(error_message), "%s", err.ToString().c_str());
            if (error_message_length < 0 ||
                error_message_length >= static_cast<int>(sizeof(error_message))) {
                snprintf(error_message, sizeof(error_message), "%s", err.Message());
            }

            char buffer[320];
            int alert_message_length =
                snprintf(buffer, sizeof(buffer), Lang::Strings::CHECK_NEW_VERSION_FAILED,
                         retry_delay, error_message);
            if (alert_message_length < 0 ||
                alert_message_length >= static_cast<int>(sizeof(buffer))) {
                snprintf(buffer, sizeof(buffer), "%s", err.Message());
            }
            Alert(Lang::Strings::ERROR, buffer, "cloud_off", Lang::Sounds::OGG_EXCLAMATION);

            ESP_LOGW(TAG, "Check new version failed, retry in %d seconds (%d/%d)", retry_delay,
                     retry_count, MAX_RETRY);
            for (int i = 0; i < retry_delay; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                if (GetDeviceState() == kDeviceStateIdle) {
                    break;
                }
            }
            retry_delay *= 2;  // Double the retry delay
            continue;
        }
        retry_count = 0;
        retry_delay = 10;  // Reset retry delay

        if (ota_->HasNewVersion()) {
            if (UpgradeFirmware(ota_->GetFirmwareUrl(), ota_->GetFirmwareVersion())) {
                return;  // This line will never be reached after reboot
            }
            // If upgrade failed, continue to normal operation
        }

        // No new version, mark the current version as valid
        ota_->MarkCurrentVersionValid();
        if (!ota_->HasActivationCode() && !ota_->HasActivationChallenge()) {
            // Exit the loop if done checking new version
            break;
        }

        display->SetStatus(Lang::Strings::ACTIVATION);
        // Activation code is shown to the user and waiting for the user to input
        if (ota_->HasActivationCode()) {
            ShowActivationCode(ota_->GetActivationCode(), ota_->GetActivationMessage());
        }

        // This will block the loop until the activation is done or timeout
        for (int i = 0; i < 10; ++i) {
            ESP_LOGI(TAG, "Activating... %d/%d", i + 1, 10);
            esp_err_t err = ota_->Activate();
            if (err == ESP_OK) {
                break;
            } else if (err == ESP_ERR_TIMEOUT) {
                vTaskDelay(pdMS_TO_TICKS(3000));
            } else {
                vTaskDelay(pdMS_TO_TICKS(10000));
            }
            if (GetDeviceState() == kDeviceStateIdle) {
                break;
            }
        }
    }
}

void Application::InitializeProtocol() {
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto codec = board.GetAudioCodec();

    display->SetStatus(Lang::Strings::LOADING_PROTOCOL);

#if CONFIG_ENGLISH_MVP_LOCAL
    protocol_ = std::make_unique<WebsocketProtocol>();
#else
    if (ota_->HasMqttConfig()) {
        protocol_ = std::make_unique<MqttProtocol>();
    } else if (ota_->HasWebsocketConfig()) {
        protocol_ = std::make_unique<WebsocketProtocol>();
    } else {
        ESP_LOGW(TAG, "No protocol specified in the OTA config, using MQTT");
        protocol_ = std::make_unique<MqttProtocol>();
    }

#endif
    protocol_->OnConnected([this]() { DismissAlert(); });

    protocol_->OnNetworkError([this](const std::string& message) {
#if CONFIG_ENGLISH_MVP_LOCAL
        Schedule([message]() { ESP_LOGW(TAG, "Local backend: %s", message.c_str()); });
#else
        last_error_message_ = message;
        xEventGroupSetBits(event_group_, MAIN_EVENT_ERROR);
#endif
    });

    protocol_->OnIncomingAudio([this](std::unique_ptr<AudioStreamPacket> packet) {
#if CONFIG_ENGLISH_MVP_LOCAL
        if (mvp_receiving_audio_.load()) {
#else
        if (GetDeviceState() == kDeviceStateSpeaking) {
#endif
            audio_service_.PushPacketToDecodeQueue(std::move(packet));
        }
    });

    protocol_->OnAudioChannelOpened([this, codec, &board]() {
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        if (protocol_->server_sample_rate() != codec->output_sample_rate()) {
            ESP_LOGW(TAG,
                     "Server sample rate %d does not match device output sample rate %d, "
                     "resampling may cause distortion",
                     protocol_->server_sample_rate(), codec->output_sample_rate());
        }
    });

    protocol_->OnAudioChannelClosed([this, &board]() {
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        Schedule([this]() {
#if CONFIG_ENGLISH_MVP_LOCAL
            // Replacing the previous socket during a connection attempt also
            // triggers this callback. The worker completion owns that outcome.
            if (mvp_connecting_) {
                return;
            }
            mvp_teaching_mode_ = "unknown";
#if CONFIG_ENGLISH_MVP_DUPLEX
            mvp_duplex_active_ = false;
            mvp_duplex_rejected_ = false;
#endif
            mvp_stopping_ = false;
            mvp_waiting_response_ = false;
            mvp_capture_started_ = false;
            mvp_receiving_audio_ = false;
            mvp_want_recording_ = false;
            mvp_playback_request_id_.clear();
            mvp_playback_started_ = false;
            mvp_playback_stop_received_ = false;
            audio_service_.FinishRawCapture();
#endif
            auto display = Board::GetInstance().GetDisplay();
            display->SetChatMessage("system", "");
            SetDeviceState(kDeviceStateIdle);
        });
    });

    protocol_->OnIncomingJson([this, display](const cJSON* root) {
        // Parse JSON data
        auto type = cJSON_GetObjectItem(root, "type");
        if (!cJSON_IsString(type)) {
            ESP_LOGW(TAG, "Incoming JSON message has no type");
            return;
        }
#if CONFIG_ENGLISH_MVP_CLOUD
        if (strcmp(type->valuestring, "mcp") == 0) {
            const auto payload = cJSON_GetObjectItem(root, "payload");
            if (cJSON_IsObject(payload))
                McpServer::GetInstance().ParseMessage(payload);
            return;
        }
#endif
#if CONFIG_ENGLISH_MVP_DUPLEX
        if (strcmp(type->valuestring, "duplex_ready") == 0) {
            const auto enabled = cJSON_GetObjectItem(root, "enabled");
            const bool accepted = cJSON_IsTrue(enabled);
            Schedule([this, accepted]() {
                mvp_duplex_active_ =
                    accepted && Board::GetInstance().GetAudioCodec()->InputReferenceHealthy();
                ESP_LOGI(TAG,
                         "Environment duplex negotiated=%d; acoustic acceptance remains required",
                         bool(mvp_duplex_active_));
                if (mvp_auto_mode_ && mvp_duplex_active_)
                    ArmDeviceVad();
            });
            return;
        }
        if (strcmp(type->valuestring, "mvp_control") == 0) {
            const auto action = cJSON_GetObjectItem(root, "action");
            if (cJSON_IsString(action) && strcmp(action->valuestring, "cancel_playback") == 0) {
                Schedule([this]() {
                    mvp_receiving_audio_ = false;
                    mvp_playback_request_id_.clear();
                    mvp_playback_started_ = false;
                    mvp_playback_stop_received_ = false;
                    audio_service_.ResetDecoder();
                    SetDeviceState(mvp_capture_started_ ? kDeviceStateListening : kDeviceStateIdle);
                });
            }
            return;
        }
        if (strcmp(type->valuestring, "input_ack") == 0)
            return;
#endif
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        if (strcmp(type->valuestring, "error") == 0) {
#if CONFIG_ENGLISH_MVP_DUPLEX
            if (mvp_duplex_active_) {
                const auto error = cJSON_GetObjectItem(root, "error");
                const auto retry = cJSON_GetObjectItem(root, "retry_after_ms");
                const bool full =
                    cJSON_IsString(error) && strcmp(error->valuestring, "voice_queue_full") == 0;
                const bool valid_retry = cJSON_IsNumber(retry) && retry->valuedouble > 0;
                const double retry_ms = valid_retry ? retry->valuedouble : 0;
                Schedule([this, full, valid_retry, retry_ms]() {
                    mvp_waiting_response_ = false;
                    mvp_stopping_ = false;
                    if (full) {
                        mvp_capture_started_ = false;
                        mvp_vad_streaming_ = false;
                        mvp_duplex_rejected_ = true;
                    }
                    if (valid_retry)
                        mvp_cloud_recovery_.PauseAfterError(esp_timer_get_time(), true, retry_ms);
                    Board::GetInstance().GetDisplay()->ShowNotification(
                        full ? "播报队列已满，请稍后再说" : "处理未完成，继续监听", 5000);
                });
                return;  // A failed/cancelled output never disables duplex input.
            }
#endif
            const auto terminal = cJSON_GetObjectItem(root, "terminal");
            if (cJSON_IsBool(terminal) && cJSON_IsFalse(terminal)) {
#if CONFIG_ENGLISH_MVP_DUPLEX
                const auto error = cJSON_GetObjectItem(root, "error");
                if (mvp_duplex_active_ && cJSON_IsString(error) &&
                    strcmp(error->valuestring, "voice_queue_full") == 0) {
                    Schedule([this]() {
                        mvp_capture_started_ = false;
                        mvp_vad_streaming_ = false;
                        mvp_duplex_rejected_ = true;
                        Board::GetInstance().GetDisplay()->ShowNotification(
                            "播报队列已满，请稍后再说", 5000);
                    });
                }
#endif
                return;  // A rejected extra command must not interrupt a valid turn.
            }
            const auto retry_after = cJSON_GetObjectItem(root, "retry_after_ms");
            const bool delay_is_number = cJSON_IsNumber(retry_after);
            const double delay_ms = delay_is_number ? retry_after->valuedouble : 0;
            mvp_receiving_audio_ = false;
            Schedule([this, delay_is_number, delay_ms]() {
                const int delay = mvp_cloud_recovery_.PauseAfterError(esp_timer_get_time(),
                                                                      delay_is_number, delay_ms);
                ResetDeviceVad();
                mvp_capture_started_ = false;
                mvp_stopping_ = false;
                mvp_waiting_response_ = false;
                mvp_want_recording_ = false;
                mvp_playback_request_id_.clear();
                mvp_playback_started_ = false;
                mvp_playback_stop_received_ = false;
                mvp_playback_reset_pending_ = false;
                audio_service_.FinishRawCapture();
                audio_service_.ResetDecoder();
                SetDeviceState(kDeviceStateIdle);
                ESP_LOGW(TAG, "Cloud turn failed; keeping listening preference, retry delay=%d ms",
                         delay);
            });
            return;
        }
#endif
#if CONFIG_ENGLISH_MVP_LOCAL
        if (strcmp(type->valuestring, "teaching_mode") == 0) {
            const auto mode = cJSON_GetObjectItem(root, "mode");
            if (cJSON_IsString(mode) && (strcmp(mode->valuestring, "chat") == 0 ||
                                         strcmp(mode->valuestring, "express") == 0)) {
                // cJSON is released by the protocol callback; copy only a validated enum.
                const bool chat = strcmp(mode->valuestring, "chat") == 0;
                Schedule([this, chat]() { UpdateMvpTeachingMode(chat ? "chat" : "express"); });
            }
            return;
        }
        if (strcmp(type->valuestring, "tts") != 0 && strcmp(type->valuestring, "stt") != 0) {
            return;
        }
#endif
        if (strcmp(type->valuestring, "notify") == 0) {
            auto audio_url = cJSON_GetObjectItem(root, "audio_url");
            if (!cJSON_IsString(audio_url) || audio_url->valuestring[0] == '\0') {
                ESP_LOGW(TAG, "Notify message requires audio_url");
                return;
            }

            std::vector<NotifySubtitle> subtitles;
            auto subtitles_json = cJSON_GetObjectItem(root, "subtitles");
            if (subtitles_json != nullptr && !cJSON_IsArray(subtitles_json)) {
                ESP_LOGW(TAG, "Notify subtitles must be an array");
                return;
            }
            if (cJSON_IsArray(subtitles_json)) {
                cJSON* item = nullptr;
                cJSON_ArrayForEach (item, subtitles_json) {
                    auto start_ms = cJSON_GetObjectItem(item, "start_ms");
                    auto text = cJSON_GetObjectItem(item, "text");
                    if (!cJSON_IsNumber(start_ms) || start_ms->valuedouble < 0 ||
                        start_ms->valuedouble > std::numeric_limits<uint32_t>::max() ||
                        !cJSON_IsString(text)) {
                        ESP_LOGW(TAG, "Ignoring invalid notify subtitle");
                        continue;
                    }
                    subtitles.push_back({.start_ms = static_cast<uint32_t>(start_ms->valuedouble),
                                         .text = text->valuestring});
                }
            }

            Schedule([this, url = std::string(audio_url->valuestring),
                      subtitles = std::move(subtitles)]() mutable {
                StartNotification(std::move(url), std::move(subtitles));
            });
        } else if (strcmp(type->valuestring, "tts") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            if (!cJSON_IsString(state)) {
                return;
            }
            if (strcmp(state->valuestring, "start") == 0) {
#if CONFIG_ENGLISH_MVP_LOCAL
                auto request_id = cJSON_GetObjectItem(root, "request_id");
                std::string playback_request_id = cJSON_IsString(request_id) ? request_id->valuestring : "";
                // The socket can deliver binary frames before the main task runs
                // the scheduled state change. Reset once here, before those frames.
                mvp_playback_reset_pending_ = true;
                audio_service_.ResetDecoder();
                mvp_receiving_audio_ = true;
#endif
                Schedule([this
#if CONFIG_ENGLISH_MVP_LOCAL
                          , playback_request_id
#endif
                          ]() {
                    aborted_ = false;
#if CONFIG_ENGLISH_MVP_LOCAL
                    mvp_playback_request_id_ = playback_request_id;
                    mvp_playback_started_ = false;
                    mvp_playback_stop_received_ = false;
                    mvp_playback_reset_pending_ = false;
                    if (mvp_auto_mode_
#if CONFIG_ENGLISH_MVP_DUPLEX
                        && !mvp_duplex_active_
#endif
                    ) {
                        mvp_capture_started_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
                        ResetDeviceVad();
#endif
                        audio_service_.FinishRawCapture();
                        mvp_waiting_response_ = true;
                        mvp_deadline_us_ = esp_timer_get_time() + 300000000LL;
                    }
#endif
                    SetDeviceState(kDeviceStateSpeaking);
                });
            } else if (strcmp(state->valuestring, "stop") == 0) {
#if CONFIG_ENGLISH_MVP_LOCAL
                auto request_id = cJSON_GetObjectItem(root, "request_id");
                std::string playback_request_id = cJSON_IsString(request_id) ? request_id->valuestring : "";
#if CONFIG_ENGLISH_MVP_DUPLEX
                if (mvp_duplex_active_ && playback_request_id != mvp_playback_request_id_) {
                    if (cJSON_IsTrue(cJSON_GetObjectItem(root, "no_audio"))) {
                        Schedule([this]() {
                            mvp_waiting_response_ = false;
                            mvp_stopping_ = false;
                            if (!mvp_capture_started_ && audio_service_.IsPlaybackIdle())
                                SetDeviceState(kDeviceStateIdle);
                        });
                    }
                    return;  // A queued/silent input completion cannot stop current output.
                }
#endif
                mvp_receiving_audio_ = false;
#endif
                Schedule([this
#if CONFIG_ENGLISH_MVP_LOCAL
                          ,
                          playback_request_id
#endif
                ]() {
#if CONFIG_ENGLISH_MVP_LOCAL
                    HandleMvpPlaybackStop(playback_request_id);
#endif
                    if (GetDeviceState() == kDeviceStateSpeaking) {
                        if (listening_mode_ == kListeningModeManualStop) {
                            SetDeviceState(kDeviceStateIdle);
                        } else {
                            SetDeviceState(kDeviceStateListening);
                        }
                    }
                });
            } else if (strcmp(state->valuestring, "sentence_start") == 0) {
                auto text = cJSON_GetObjectItem(root, "text");
                if (cJSON_IsString(text)) {
                    std::vector<TextGlyph> glyphs;
                    uint8_t bpp = 0;
                    if (!TextGlyphPayload::Parse(root, glyphs, bpp)) {
                        glyphs.clear();
                    }
                    ESP_LOGI(TAG, "<< %s", text->valuestring);
                    Schedule([display, message = std::string(text->valuestring),
                              glyphs = std::move(glyphs), bpp]() {
                        display->AddTextGlyphs(glyphs, bpp);
                        display->SetChatMessage("assistant", message.c_str());
                    });
                }
            }
        } else if (strcmp(type->valuestring, "stt") == 0) {
            auto text = cJSON_GetObjectItem(root, "text");
            if (cJSON_IsString(text)) {
                std::vector<TextGlyph> glyphs;
                uint8_t bpp = 0;
                if (!TextGlyphPayload::Parse(root, glyphs, bpp)) {
                    glyphs.clear();
                }
                ESP_LOGI(TAG, ">> %s", text->valuestring);
                Schedule([display, message = std::string(text->valuestring),
                          glyphs = std::move(glyphs), bpp]() {
                    display->AddTextGlyphs(glyphs, bpp);
                    display->SetChatMessage("user", message.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "llm") == 0) {
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(emotion)) {
                Schedule([display, emotion_str = std::string(emotion->valuestring)]() {
                    display->SetEmotion(emotion_str.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "mcp") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            if (cJSON_IsObject(payload)) {
                McpServer::GetInstance().ParseMessage(payload);
            }
        } else if (strcmp(type->valuestring, "system") == 0) {
            auto command = cJSON_GetObjectItem(root, "command");
            if (cJSON_IsString(command)) {
                ESP_LOGI(TAG, "System command: %s", command->valuestring);
                if (strcmp(command->valuestring, "reboot") == 0) {
                    // Do a reboot if user requests a OTA update
                    Schedule([this]() { Reboot(); });
                } else {
                    ESP_LOGW(TAG, "Unknown system command: %s", command->valuestring);
                }
            }
        } else if (strcmp(type->valuestring, "alert") == 0) {
            auto status = cJSON_GetObjectItem(root, "status");
            auto message = cJSON_GetObjectItem(root, "message");
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(status) && cJSON_IsString(message) && cJSON_IsString(emotion)) {
                Alert(status->valuestring, message->valuestring, emotion->valuestring,
                      Lang::Sounds::OGG_VIBRATION);
            } else {
                ESP_LOGW(TAG, "Alert command requires status, message and emotion");
            }
#if CONFIG_RECEIVE_CUSTOM_MESSAGE
        } else if (strcmp(type->valuestring, "custom") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            CJsonStringUniquePtr root_json(cJSON_PrintUnformatted(root));
            ESP_LOGI(TAG, "Received custom message: %s", root_json ? root_json.get() : "");
            if (cJSON_IsObject(payload)) {
                CJsonStringUniquePtr payload_json(cJSON_PrintUnformatted(payload));
                if (payload_json) {
                    Schedule([this, display, payload_str = std::string(payload_json.get())]() {
                        display->SetChatMessage("system", payload_str.c_str());
                    });
                }
            } else {
                ESP_LOGW(TAG, "Invalid custom message format: missing payload");
            }
#endif
        } else {
            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);
        }
    });

    protocol_->Start();
}

void Application::ShowActivationCode(const std::string& code, const std::string& message) {
    struct digit_sound {
        char digit;
        const std::string_view& sound;
    };
    static const std::array<digit_sound, 10> digit_sounds{
        {digit_sound{'0', Lang::Sounds::OGG_0}, digit_sound{'1', Lang::Sounds::OGG_1},
         digit_sound{'2', Lang::Sounds::OGG_2}, digit_sound{'3', Lang::Sounds::OGG_3},
         digit_sound{'4', Lang::Sounds::OGG_4}, digit_sound{'5', Lang::Sounds::OGG_5},
         digit_sound{'6', Lang::Sounds::OGG_6}, digit_sound{'7', Lang::Sounds::OGG_7},
         digit_sound{'8', Lang::Sounds::OGG_8}, digit_sound{'9', Lang::Sounds::OGG_9}}};

    // This sentence uses 9KB of SRAM, so we need to wait for it to finish
    Alert(Lang::Strings::ACTIVATION, message.c_str(), "link", Lang::Sounds::OGG_ACTIVATION);

    for (const auto& digit : code) {
        auto it = std::find_if(digit_sounds.begin(), digit_sounds.end(),
                               [digit](const digit_sound& ds) { return ds.digit == digit; });
        if (it != digit_sounds.end()) {
            audio_service_.PlaySound(it->sound);
        }
    }
}

void Application::Alert(const char* status, const char* message, const char* emotion,
                        const std::string_view& sound) {
    ESP_LOGW(TAG, "Alert [%s] %s: %s", emotion, status, message);
    auto display = Board::GetInstance().GetDisplay();
    display->SetStatus(status);
    display->SetEmotion(emotion);
    display->SetChatMessage("system", message);
    if (!sound.empty()) {
        audio_service_.PlaySound(sound);
    }
}

void Application::DismissAlert() {
    last_error_message_.clear();
    if (GetDeviceState() == kDeviceStateIdle) {
        auto display = Board::GetInstance().GetDisplay();
        display->SetStatus(Lang::Strings::STANDBY);
        display->SetEmotion("neutral");
        display->SetChatMessage("system", "");
    }
}

void Application::ToggleChatState() { xEventGroupSetBits(event_group_, MAIN_EVENT_TOGGLE_CHAT); }

void Application::StartListening() { xEventGroupSetBits(event_group_, MAIN_EVENT_START_LISTENING); }

void Application::StopListening() { xEventGroupSetBits(event_group_, MAIN_EVENT_STOP_LISTENING); }

void Application::HandleToggleChatEvent() {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_) {
        CancelEnvironmentPlayback();
        return;
    }
#endif
#if CONFIG_ENGLISH_MVP_LOCAL
    if (GetDeviceState() == kDeviceStateSpeaking) {
        InterruptMvpPlayback();
        return;
    }
    if (mvp_auto_mode_) {
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        if (mvp_vad_streaming_) {
            mvp_auto_mode_ = false;
            mvp_cloud_recovery_.Clear();
            HandleStopListeningEvent();  // End this turn and return to manual mode.
            ESP_LOGI(TAG, "Manual button mode requested during automatic turn");
            return;
        }
        if (mvp_stopping_ || mvp_waiting_response_ || GetDeviceState() == kDeviceStateSpeaking) {
            return;
        }
#endif
        ToggleMvpAutoMode();  // One click always returns to the button fallback.
        return;
    }
    if (GetDeviceState() == kDeviceStateWifiConfiguring ||
        GetDeviceState() == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        ESP_LOGW(TAG, "Recording unavailable during WiFi setup; local echo disabled");
        return;
    }
    if (mvp_connecting_) {
        mvp_want_recording_ = !mvp_want_recording_;
        return;
    }
    if (mvp_stopping_ || mvp_waiting_response_) {
        return;
    }
    if (GetDeviceState() == kDeviceStateIdle &&
        (!audio_service_.IsUplinkDrained() || !audio_service_.IsPlaybackIdle())) {
        return;
    }
#endif
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
        state = kDeviceStateIdle;
    }

    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
        ListeningMode mode = GetDefaultListeningMode();
        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
#if CONFIG_ENGLISH_MVP_LOCAL
            mvp_want_recording_ = true;
            StartMvpConnection();
#else
            // Schedule to let the state change be processed first (UI update)
            Schedule([this, mode]() { ContinueOpenAudioChannel(mode); });
#endif
            return;
        }
        SetListeningMode(mode);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
    } else if (state == kDeviceStateListening) {
#if CONFIG_ENGLISH_MVP_LOCAL
        HandleStopListeningEvent();
#else
        protocol_->CloseAudioChannel();
#endif
    }
}

void Application::ContinueOpenAudioChannel(ListeningMode mode) {
#if CONFIG_ENGLISH_MVP_LOCAL
    StartMvpConnection();
    return;
#endif
    // Check state again in case it was changed during scheduling
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    // Switch to performance mode before connecting to reduce latency
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);

    if (!protocol_->IsAudioChannelOpened()) {
        if (!protocol_->OpenAudioChannel()) {
            // Return to idle so the device is not stuck in the connecting
            // state (not every failure path reports a network error)
            SetDeviceState(kDeviceStateIdle);
            return;
        }
    }

    SetListeningMode(mode);
}

void Application::HandleStartListeningEvent() {
#if CONFIG_ENGLISH_MVP_LOCAL
    if (GetDeviceState() == kDeviceStateWifiConfiguring ||
        GetDeviceState() == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        ESP_LOGW(TAG, "Recording unavailable during WiFi setup; local echo disabled");
        return;
    }
    if (mvp_connecting_) {
        mvp_want_recording_ = true;
        return;
    }
    if (mvp_stopping_ || mvp_waiting_response_ || !audio_service_.IsUplinkDrained()) {
        return;
    }
    if (!audio_service_.IsPlaybackIdle()) {
        return;
    }
#endif
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
        state = kDeviceStateIdle;
    }

    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
#if CONFIG_ENGLISH_MVP_LOCAL
            mvp_want_recording_ = true;
            StartMvpConnection();
#else
            // Schedule to let the state change be processed first (UI update)
            Schedule([this]() { ContinueOpenAudioChannel(kListeningModeManualStop); });
#endif
            return;
        }
        SetListeningMode(kListeningModeManualStop);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
        SetListeningMode(kListeningModeManualStop);
    }
}

void Application::HandleStopListeningEvent() {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_ && mvp_capture_started_) {
        FinishEnvironmentTurn();
        return;
    }
#endif
#if CONFIG_ENGLISH_MVP_LOCAL
    mvp_want_recording_ = false;
    if (GetDeviceState() == kDeviceStateConnecting || mvp_connecting_) {
        // A short press must not start capture after a slow connection completes.
        SetDeviceState(kDeviceStateIdle);
        return;
    }
#endif
    auto state = GetDeviceState();

    if (state == kDeviceStateNotifying) {
        StopNotification();
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    } else if (state == kDeviceStateListening) {
#if CONFIG_ENGLISH_MVP_LOCAL
        if (!mvp_stopping_) {
            mvp_stopping_ = true;
            mvp_deadline_us_ = esp_timer_get_time() + 3000000;
            audio_service_.FinishRawCapture();
        }
#else
        if (protocol_) {
            protocol_->SendStopListening();
        }
        SetDeviceState(kDeviceStateIdle);
#endif
    }
}

#if CONFIG_ENGLISH_MVP_LOCAL
void Application::UpdateMvpTeachingMode(const char* mode) {
    if (!mode || (strcmp(mode, "chat") != 0 && strcmp(mode, "express") != 0)) {
        return;
    }
    mvp_teaching_mode_ = strcmp(mode, "chat") == 0 ? "chat" : "express";
    Board::GetInstance().GetDisplay()->UpdateStatusBar(true);
}

const char* Application::GetMvpDisplayStatus() const {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_reference_fault_notified_)
        return "Audio error";
    if (mvp_duplex_active_ && mvp_vad_monitoring_ &&
        (GetDeviceState() == kDeviceStateSpeaking || mvp_receiving_audio_))
        return "Playing + listening";
#endif
    if (GetDeviceState() == kDeviceStateWifiConfiguring) {
        return "Wi-Fi setup";
    }
    if (!mvp_network_ready_) {
        return "Connecting Wi-Fi";
    }
    if (mvp_connecting_ || !protocol_ || !protocol_->IsAudioChannelOpened()) {
        return "Connecting cloud";
    }
    if (GetDeviceState() == kDeviceStateSpeaking || mvp_receiving_audio_) {
        return "Playing";
    }
    if (mvp_waiting_response_) {
        return "Processing";
    }
    if (mvp_stopping_) {
        return "Uploading";
    }
    if (mvp_capture_started_) {
        return "Recording";
    }
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    if (!mvp_cloud_recovery_.CanListen(esp_timer_get_time())) {
        return "Paused";
    }
    if (mvp_vad_monitoring_) {
        return "Listening";
    }
#endif
    return "Ready";
}

bool Application::SetRemoteMvpAutoMode(bool enabled) {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (enabled && !Board::GetInstance().GetAudioCodec()->InputReferenceHealthy())
        return false;
#endif
    // MCP runs this on the main task. Refuse an in-flight turn and retain the
    // authenticated channel so button mode can also receive remote controls.
    if (GetDeviceState() != kDeviceStateIdle || mvp_capture_started_ || mvp_waiting_response_ ||
        mvp_stopping_ || !audio_service_.IsPlaybackIdle()) {
        return false;
    }
#if !CONFIG_USE_AUDIO_PROCESSOR
    if (enabled)
        return false;
#endif
    if (enabled != mvp_auto_mode_) {
        mvp_auto_mode_ = enabled;
        if (enabled) {
            mvp_auto_resume_at_us_ = 0;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
            mvp_cloud_recovery_.Clear();
            ArmDeviceVad();
#endif
        } else {
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
            ResetDeviceVad();
#endif
            audio_service_.FinishRawCapture();
        }
    }
    Board::GetInstance().GetDisplay()->UpdateStatusBar(true);
    return mvp_auto_mode_ == enabled;
}

void Application::ToggleMvpAutoMode() {
#if CONFIG_ENGLISH_MVP_CLOUD && !CONFIG_USE_AUDIO_PROCESSOR
    // Cloud phase one has no server Silero. Never upload idle-room audio continuously.
    ESP_LOGW(TAG, "Cloud button mode: automatic listening not enabled yet");
    return;
#endif
    // Board callbacks schedule this on the main task, like the existing buttons.
    if (!mvp_auto_mode_) {
        if (GetDeviceState() != kDeviceStateIdle || mvp_waiting_response_ || mvp_stopping_) {
            ESP_LOGI(TAG, "Wait until idle before enabling automatic listening");
            return;
        }
        mvp_auto_mode_ = true;
        mvp_auto_resume_at_us_ = esp_timer_get_time() + 300000;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        mvp_cloud_recovery_.Clear();
        ESP_LOGI(TAG, "Device VAD armed; speech starts a bounded manual WS turn");
#else
        ESP_LOGI(TAG, "Automatic listening enabled (server Silero); click to return to manual");
#endif
        PlaySound(Lang::Sounds::OGG_SUCCESS);
        return;
    }
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    if (mvp_vad_streaming_) {
        mvp_auto_mode_ = false;
        mvp_cloud_recovery_.Clear();
        HandleStopListeningEvent();
        ESP_LOGI(TAG, "Manual button mode requested during automatic turn");
        return;
    }
    if (mvp_stopping_ || mvp_waiting_response_ || GetDeviceState() == kDeviceStateSpeaking) {
        ESP_LOGI(TAG, "Wait for the current turn before returning to manual mode");
        return;
    }
#endif
    mvp_auto_mode_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    mvp_cloud_recovery_.Clear();
    ResetDeviceVad();
#endif
    mvp_capture_started_ = false;
    mvp_waiting_response_ = false;
    mvp_stopping_ = false;
    mvp_want_recording_ = false;
    mvp_receiving_audio_ = false;
    audio_service_.FinishRawCapture();
    audio_service_.ResetDecoder();
    if (protocol_ && !mvp_connecting_) {
        protocol_->CloseAudioChannel();
    }
    SetDeviceState(kDeviceStateIdle);
    ESP_LOGI(TAG, "Manual button mode restored");
}

void Application::FinishMvpUpload() {
    if (!mvp_stopping_ || !audio_service_.IsUplinkDrained()) {
        return;
    }
    mvp_stopping_ = false;
    if (mvp_capture_started_ && protocol_ && !mvp_connecting_ &&
        protocol_->IsAudioChannelOpened()) {
        protocol_->SendStopListening();
        mvp_waiting_response_ = true;
        mvp_deadline_us_ = esp_timer_get_time() + 300000000LL;
    }
    mvp_capture_started_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    ResetDeviceVad();
#endif
    SetDeviceState(kDeviceStateIdle);
}

#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
void Application::ResetDeviceVad() {
    mvp_vad_monitoring_ = false;
    mvp_vad_streaming_ = false;
    mvp_vad_silence_since_us_ = 0;
    mvp_vad_preroll_.clear();
}

#if CONFIG_ENGLISH_MVP_DUPLEX
void Application::FinishEnvironmentTurn() {
    if (!mvp_capture_started_ || !protocol_)
        return;
    const auto submitted = esp_timer_get_time();
    const auto ended = mvp_vad_silence_since_us_ ? mvp_vad_silence_since_us_ : submitted;
    // All currently popped Opus frames precede this control message on the
    // same ordered socket. Keep AFE/encoding running; later frames belong to
    // bounded pre-roll. The 1.2 s quiet tail protects the speech boundary.
    protocol_->SendEnvironmentStop(ended, submitted);
    mvp_capture_started_ = false;
    mvp_vad_streaming_ = false;
    mvp_vad_silence_since_us_ = 0;
    ESP_LOGI(TAG,
             "{\"event\":\"duplex_input_end\",\"speech_end_us\":%lld,\"submitted_us\":%lld,"
             "\"basis\":\"vad_edge_not_acoustic\"}",
             ended, submitted);
    if (GetDeviceState() != kDeviceStateSpeaking)
        SetDeviceState(kDeviceStateIdle);
}

void Application::CancelEnvironmentPlayback() {
    if (protocol_ && protocol_->IsAudioChannelOpened())
        protocol_->SendAbortSpeaking(kAbortReasonNone);
    mvp_receiving_audio_ = false;
    mvp_playback_request_id_.clear();
    mvp_playback_started_ = false;
    mvp_playback_stop_received_ = false;
    audio_service_.ResetDecoder();
    SetDeviceState(mvp_capture_started_ ? kDeviceStateListening : kDeviceStateIdle);
    ESP_LOGI(TAG, "Environment output cancelled; input capture remains enabled");
}
#endif

void Application::ArmDeviceVad() {
    if (!mvp_auto_mode_ || mvp_vad_monitoring_ || !protocol_ ||
        !protocol_->IsAudioChannelOpened() ||
        !mvp_cloud_recovery_.CanListen(esp_timer_get_time())) {
        return;
    }
    mvp_vad_preroll_.clear();
    mvp_vad_silence_since_us_ = 0;
    mvp_vad_monitoring_ = true;
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_)
        audio_service_.EnableVoiceProcessing(true, false);
    else
#endif
        audio_service_.EnableVoiceProcessing(true);
    if (!audio_service_.IsAudioProcessorRunning()) {
        ResetDeviceVad();
        ESP_LOGE(TAG, "Device VAD audio processor failed to start");
        return;
    }
    ESP_LOGI(TAG, "Device VAD monitoring; no audio sent before speech");
}

void Application::HandleDeviceVadChange(bool speaking) {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_ && mvp_auto_mode_ && mvp_vad_monitoring_) {
        if (!speaking) {
            if ((mvp_vad_streaming_ || mvp_duplex_rejected_) && !mvp_vad_silence_since_us_) {
                mvp_vad_silence_since_us_ = esp_timer_get_time();
            }
            return;
        }
        mvp_vad_silence_since_us_ = 0;
        if (mvp_capture_started_ || mvp_duplex_rejected_ || !protocol_->IsAudioChannelOpened() ||
            !mvp_cloud_recovery_.CanListen(esp_timer_get_time()))
            return;
        const auto now = esp_timer_get_time();
        protocol_->SendEnvironmentStart(now);
        mvp_capture_started_ = true;
        mvp_vad_streaming_ = true;
        mvp_deadline_us_ = now + 13000000;
        ESP_LOGI(TAG, "{\"event\":\"duplex_upload_started\",\"device_us\":%lld}", now);
        while (!mvp_vad_preroll_.empty()) {
            auto packet = std::move(mvp_vad_preroll_.front());
            mvp_vad_preroll_.pop_front();
            if (!protocol_->SendAudio(std::move(packet))) {
                ESP_LOGE(TAG, "Duplex upload failed; captured input was not delivered");
                protocol_->CloseAudioChannel();
                break;
            }
        }
        return;  // VAD never cancels or resets playback.
    }
#endif
    if (!mvp_auto_mode_ || !mvp_vad_monitoring_ || mvp_stopping_ || mvp_waiting_response_ ||
        !mvp_cloud_recovery_.CanListen(esp_timer_get_time())) {
        return;
    }
    if (!speaking) {
        if (mvp_vad_streaming_ && mvp_vad_silence_since_us_ == 0) {
            mvp_vad_silence_since_us_ = esp_timer_get_time();
        }
        return;
    }
    mvp_vad_silence_since_us_ = 0;
    if (mvp_vad_streaming_ || GetDeviceState() != kDeviceStateIdle || !protocol_ ||
        !protocol_->IsAudioChannelOpened() || !audio_service_.IsPlaybackIdle()) {
        return;
    }
    // The cloud server has no Silero instance. Start its existing manual
    // request only after the device VAD sees speech, then flush about 2 s pre-roll.
    mvp_vad_streaming_ = true;
    mvp_capture_started_ = true;
    mvp_deadline_us_ = esp_timer_get_time() + 13000000;
    protocol_->SendStartListening(kListeningModeManualStop);
    while (!mvp_vad_preroll_.empty()) {
        auto packet = std::move(mvp_vad_preroll_.front());
        mvp_vad_preroll_.pop_front();
        if (!protocol_->SendAudio(std::move(packet))) {
            ESP_LOGE(TAG, "Device VAD pre-roll send failed");
            ResetDeviceVad();
            mvp_capture_started_ = false;
            audio_service_.FinishRawCapture();
            protocol_->CloseAudioChannel();
            SetDeviceState(kDeviceStateIdle);
            return;
        }
    }
    listening_mode_ = kListeningModeManualStop;
    SetDeviceState(kDeviceStateListening);
    ESP_LOGI(TAG, "Device VAD speech started");
}

void Application::MaybeFinishDeviceVadTurn() {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_) {
        if (mvp_vad_silence_since_us_ &&
            esp_timer_get_time() - mvp_vad_silence_since_us_ >= 1200000) {
            if (mvp_capture_started_)
                FinishEnvironmentTurn();
            mvp_duplex_rejected_ = false;
            mvp_vad_preroll_.clear();
            mvp_vad_silence_since_us_ = 0;
        }
        return;
    }
#endif
    if (!mvp_vad_streaming_ || mvp_stopping_ || mvp_vad_silence_since_us_ == 0) {
        return;
    }
    if (esp_timer_get_time() - mvp_vad_silence_since_us_ >= 1200000) {
        ESP_LOGI(TAG, "Device VAD speech ended after 1200 ms quiet");
        HandleStopListeningEvent();
    }
}
#endif

void Application::StartMvpConnection() {
    if (mvp_connecting_) {
        return;
    }
    if (!mvp_network_ready_ || !protocol_) {
        mvp_want_recording_ = false;
        if (GetDeviceState() == kDeviceStateConnecting) {
            SetDeviceState(kDeviceStateIdle);
        }
        return;
    }
    mvp_connecting_ = true;
    mvp_teaching_mode_ = "unknown";
    mvp_connect_epoch_ = mvp_network_epoch_;
    Board::GetInstance().SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    auto result = xTaskCreate(
        [](void* arg) {
            auto app = static_cast<Application*>(arg);
            const auto epoch = app->mvp_connect_epoch_;
            app->protocol_->CloseAudioChannel();
            const bool opened = app->protocol_->OpenAudioChannel();
            app->Schedule([app, opened, epoch]() {
                app->mvp_connecting_ = false;
                if (app->mvp_reset_pending_) {
                    app->mvp_reset_pending_ = false;
                    app->ResetProtocol();
                    return;
                }
                if (!opened || !app->mvp_network_ready_ || epoch != app->mvp_network_epoch_ ||
                    !app->protocol_->IsAudioChannelOpened()) {
                    app->protocol_->CloseAudioChannel();
                    app->mvp_want_recording_ = false;
                    app->SetDeviceState(kDeviceStateIdle);
                    app->mvp_retry_at_us_ =
                        esp_timer_get_time() + app->mvp_retry_seconds_ * 1000000LL;
                    app->mvp_retry_seconds_ = std::min(30, app->mvp_retry_seconds_ * 2);
                    return;
                }
                app->mvp_retry_seconds_ = 1;
                app->mvp_ping_at_us_ = esp_timer_get_time() + 15000000;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
                ESP_LOGI(TAG, "Cloud backend connected; device VAD is opt-in");
#else
                ESP_LOGI(TAG, "Local backend connected; microphone remains button-controlled");
#endif
                if (app->mvp_want_recording_ && app->audio_service_.IsUplinkDrained() &&
                    app->audio_service_.IsPlaybackIdle()) {
                    app->mvp_want_recording_ = false;
                    app->SetListeningMode(kListeningModeManualStop);
                } else {
                    app->SetDeviceState(kDeviceStateIdle);
                }
            });
            vTaskDelete(nullptr);
        },
        "mvp_connect", 8192, this, 2, nullptr);
    if (result != pdPASS) {
        mvp_connecting_ = false;
        mvp_want_recording_ = false;
        mvp_retry_at_us_ = esp_timer_get_time() + 30000000;
        SetDeviceState(kDeviceStateIdle);
        ESP_LOGE(TAG, "Cannot allocate local connection task");
    }
}

void Application::MvpNetworkTick() {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (!Board::GetInstance().GetAudioCodec()->InputReferenceHealthy()) {
        if (!mvp_reference_fault_notified_) {
            mvp_reference_fault_notified_ = true;
            ESP_LOGE(TAG,
                     "AEC reference fault: duplex disabled explicitly, no acoustic acceptance");
            CancelEnvironmentPlayback();
            mvp_duplex_active_ = false;
            ResetDeviceVad();
            audio_service_.FinishRawCapture();
            if (protocol_)
                protocol_->CloseAudioChannel();
            Board::GetInstance().GetDisplay()->ShowNotification("AEC参考异常，请检查设备", 10000);
        }
        return;
    }
#endif
    if (!protocol_ || !mvp_network_ready_ || mvp_connecting_) {
        return;
    }
    const auto current_state = GetDeviceState();
    if (current_state != kDeviceStateIdle && current_state != kDeviceStateConnecting &&
        current_state != kDeviceStateListening && current_state != kDeviceStateSpeaking) {
        return;
    }
    const auto now = esp_timer_get_time();
    if (protocol_->IsAudioChannelOpened()) {
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        if (mvp_vad_monitoring_ && !mvp_vad_streaming_ &&
            !audio_service_.IsAudioProcessorRunning()) {
            ESP_LOGW(TAG, "Device VAD processor stopped unexpectedly; rearming");
            ResetDeviceVad();
            audio_service_.FinishRawCapture();
        }
#endif
        MaybeResumeMvpAutoListening();
        if (now >= mvp_ping_at_us_) {
            protocol_->SendPing();
            mvp_ping_at_us_ = now + 15000000;
        }
        return;
    }
    auto state = GetDeviceState();
    if (state == kDeviceStateListening || state == kDeviceStateSpeaking || mvp_waiting_response_) {
        mvp_stopping_ = false;
        mvp_waiting_response_ = false;
        mvp_capture_started_ = false;
        mvp_receiving_audio_ = false;
        audio_service_.FinishRawCapture();
        SetDeviceState(kDeviceStateIdle);
    }
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    if (mvp_vad_monitoring_) {
        ResetDeviceVad();
        audio_service_.FinishRawCapture();
    }
#endif
    if (GetDeviceState() == kDeviceStateIdle && audio_service_.IsUplinkDrained() &&
        now >= mvp_retry_at_us_) {
        StartMvpConnection();
    }
}
#endif

void Application::HandleWakeWordDetectedEvent() {
    if (!protocol_) {
        return;
    }

    auto state = GetDeviceState();
    auto wake_word = audio_service_.GetLastWakeWord();
    ESP_LOGI(TAG, "Wake word detected: %s (state: %d)", wake_word.c_str(), (int)state);

    if (state == kDeviceStateIdle) {
        BeginWakeWordInvoke(wake_word);
    } else if (state == kDeviceStateNotifying) {
        StopNotification();
        BeginWakeWordInvoke(wake_word);
    } else if (state == kDeviceStateSpeaking || state == kDeviceStateListening) {
        AbortSpeaking(kAbortReasonWakeWordDetected);
        // Clear send queue to avoid sending residues to server
        while (audio_service_.PopPacketFromSendQueue())
            ;

        if (state == kDeviceStateListening) {
            protocol_->SendStartListening(GetDefaultListeningMode());
            audio_service_.ResetDecoder();
            audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
            // Re-enable wake word detection as it was stopped by the detection itself
            audio_service_.EnableWakeWordDetection(true);
        } else {
            // Play popup sound and start listening again
            play_popup_on_listening_ = true;
            SetListeningMode(GetDefaultListeningMode());
        }
    } else if (state == kDeviceStateActivating) {
        // Restart the activation check if the wake word is detected during activation
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::BeginWakeWordInvoke(const std::string& wake_word) {
    // Must run in the main task with the device in idle state
    audio_service_.EncodeWakeWord();

    // Always pass through the connecting state, even if the audio channel is
    // already opened. ContinueWakeWordInvoke() rejects any other state, so
    // skipping this transition would silently drop the wake word invocation.
    if (!SetDeviceState(kDeviceStateConnecting)) {
        // Wake word detection was stopped by the detection itself; restore it
        // so the device does not become unresponsive to wake words.
        audio_service_.EnableWakeWordDetection(true);
        return;
    }

    if (!protocol_->IsAudioChannelOpened()) {
        // Schedule to let the state change be processed first (UI update),
        // then continue with OpenAudioChannel which may block for ~1 second
        Schedule([this, wake_word]() { ContinueWakeWordInvoke(wake_word); });
        return;
    }
    // Channel already opened, continue directly
    ContinueWakeWordInvoke(wake_word);
}

void Application::ContinueWakeWordInvoke(const std::string& wake_word) {
    // Check state again in case it was changed during scheduling
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    // Switch to performance mode before connecting to reduce latency
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);

    if (!protocol_->IsAudioChannelOpened()) {
        if (!protocol_->OpenAudioChannel()) {
            // Return to idle so the device is not stuck in the connecting
            // state (not every failure path reports a network error), and
            // wake word detection is re-enabled by the idle state handler.
            SetDeviceState(kDeviceStateIdle);
            return;
        }
    }

    ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
#if CONFIG_SEND_WAKE_WORD_DATA
    // Encode and send the wake word data to the server
    while (auto packet = audio_service_.PopWakeWordPacket()) {
        protocol_->SendAudio(std::move(packet));
    }
    // Set the chat state to wake word detected
    protocol_->SendWakeWordDetected(wake_word);
    SetListeningMode(GetDefaultListeningMode());
#else
    // Set flag to play popup sound after state changes to listening
    // (PlaySound here would be cleared by ResetDecoder in EnableVoiceProcessing)
    play_popup_on_listening_ = true;
    SetListeningMode(GetDefaultListeningMode());
#endif
}

void Application::HandleStateChangedEvent() {
    DeviceState new_state = state_machine_.GetState();
    clock_ticks_ = 0;
    // Any state change invalidates a pending deferred listening start;
    // the Listening case below re-arms it when needed.
    pending_listening_start_ = false;

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto led = board.GetLed();
    led->OnStateChanged();

    switch (new_state) {
        case kDeviceStateUnknown:
        case kDeviceStateIdle:
            // Keep a just-raised network error visible. SetDeviceState(idle)
            // queues STATE_CHANGED after Alert(), and the idle handler would
            // otherwise wipe the status, emotion, and chat message.
            if (last_error_message_.empty()) {
                display->SetStatus(Lang::Strings::STANDBY);
                display->ClearChatMessages();  // Clear messages first
                display->SetEmotion(
                    "neutral");  // Then set emotion (wechat mode checks child count)
            }
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
            if (!mvp_vad_monitoring_) {
                audio_service_.EnableVoiceProcessing(false);
            }
#else
            audio_service_.EnableVoiceProcessing(false);
#endif
            audio_service_.EnableWakeWordDetection(true);
            break;
        case kDeviceStateConnecting:
            display->SetStatus(Lang::Strings::CONNECTING);
            display->SetEmotion("neutral");
            display->SetChatMessage("system", "");
            break;
        case kDeviceStateListening:
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");

            // Make sure the audio processor is running
            if (play_popup_on_listening_ || !audio_service_.IsAudioProcessorRunning()) {
                // For auto mode, wait for the playback queue to drain before enabling
                // voice processing. This prevents audio truncation when STOP arrives
                // late due to network jitter. Instead of blocking the main loop here,
                // defer the start until MAIN_EVENT_PLAYBACK_DRAINED arrives.
                if (listening_mode_ == kListeningModeAutoStop && !audio_service_.IsPlaybackIdle()) {
                    pending_listening_start_ = true;
                } else {
                    StartListeningAudio();
                }
            } else {
                ConfigureWakeWordForListening();
            }
            break;
        case kDeviceStateSpeaking:
            display->SetStatus(Lang::Strings::SPEAKING);

            if (listening_mode_ != kListeningModeRealtime
#if CONFIG_ENGLISH_MVP_DUPLEX
                && !mvp_duplex_active_
#endif
            ) {
                audio_service_.EnableVoiceProcessing(false);
                // Only AFE wake word can be detected in speaking mode
                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            }
#if !CONFIG_ENGLISH_MVP_LOCAL
            audio_service_.ResetDecoder();
#endif
            break;
        case kDeviceStateNotifying:
            display->SetStatus(Lang::Strings::SPEAKING);
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
            break;
        case kDeviceStateWifiConfiguring:
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(false);
            break;
        default:
            // Do nothing
            break;
    }
}

void Application::StartListeningAudio() {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_) {
        ArmDeviceVad();
        return;
    }
#endif
    // Runs in the main loop, either directly from HandleStateChangedEvent or
    // deferred via MAIN_EVENT_PLAYBACK_DRAINED once the playback queue drains.
    if (GetDeviceState() != kDeviceStateListening) {
        return;
    }
#if CONFIG_ENGLISH_MVP_LOCAL
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    if (mvp_vad_streaming_) {
        // Monitoring already owns the AFE. The manual start and pre-roll were
        // sent at the VAD edge, so a second start would discard the first words.
        return;
    }
#endif
    // A release may arrive before the queued state-change event.
    if (mvp_stopping_) {
        return;
    }
    mvp_capture_started_ = true;
    // The server accepts at most 15 s. Allow a tick and one padded frame of headroom.
    mvp_deadline_us_ = esp_timer_get_time() + 13000000;
#endif

    // Send the start listening command
    protocol_->SendStartListening(listening_mode_);
    audio_service_.EnableVoiceProcessing(true);

    ConfigureWakeWordForListening();

    // Play popup sound after ResetDecoder (in EnableVoiceProcessing) has been called
    if (play_popup_on_listening_) {
        play_popup_on_listening_ = false;
        audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
    }
}

void Application::ConfigureWakeWordForListening() {
#ifdef CONFIG_WAKE_WORD_DETECTION_IN_LISTENING
    // Enable wake word detection in listening mode (configured via Kconfig)
    audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
#else
    // Disable wake word detection in listening mode
    audio_service_.EnableWakeWordDetection(false);
#endif
}

void Application::StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles) {
    if (GetDeviceState() != kDeviceStateIdle || notify_player_.IsBusy()) {
        ESP_LOGW(TAG, "Ignoring notify message while device is busy");
        return;
    }

    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.EnableVoiceProcessing(false);
    audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
    audio_service_.ReleaseWakeWordResources();
    while (audio_service_.PopPacketFromSendQueue()) {
        // Discard microphone audio left over from a previous conversation.
    }

    if (!SetDeviceState(kDeviceStateNotifying)) {
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        return;
    }

    audio_service_.ResetDecoder();
    uint32_t playback_id = ++notification_playback_id_;
    if (playback_id == 0) {
        playback_id = ++notification_playback_id_;
    }
    audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);

    bool started = notify_player_.Start(
        std::move(audio_url), std::move(subtitles), playback_id,
        [this](uint32_t id, const std::string& text) {
            Schedule([this, id, text]() {
                if (GetDeviceState() == kDeviceStateNotifying && notification_playback_id_ == id) {
                    Board::GetInstance().GetDisplay()->SetChatMessage("assistant", text.c_str());
                }
            });
        },
        [this](uint32_t id, bool success) {
            Schedule([this, id, success]() { HandleNotificationFinished(id, success); });
        });

    if (!started) {
        ESP_LOGE(TAG, "Failed to start notification playback");
        StopNotification();
    }
}

void Application::StopNotification() {
    notify_player_.Stop();
    audio_service_.ResetDecoder();
    auto& board = Board::GetInstance();
    board.GetDisplay()->SetChatMessage("assistant", "");
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
    if (GetDeviceState() == kDeviceStateNotifying) {
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::HandleNotificationFinished(uint32_t playback_id, bool success) {
    if (GetDeviceState() != kDeviceStateNotifying || notification_playback_id_ != playback_id) {
        return;
    }
    ESP_LOGI(TAG, "Notification playback %lu %s", static_cast<unsigned long>(playback_id),
             success ? "completed" : "failed");
    StopNotification();
}

void Application::Schedule(std::function<void()>&& callback) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        main_tasks_.push_back(std::move(callback));
    }
    xEventGroupSetBits(event_group_, MAIN_EVENT_SCHEDULE);
}

void Application::AbortSpeaking(AbortReason reason) {
    ESP_LOGI(TAG, "Abort speaking");
    aborted_ = true;
    if (protocol_) {
        protocol_->SendAbortSpeaking(reason);
    }
}

#if CONFIG_ENGLISH_MVP_LOCAL
void Application::MaybeResumeMvpAutoListening() {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_) {
        if (mvp_auto_mode_ && !mvp_vad_monitoring_)
            ArmDeviceVad();
        return;
    }
#endif
    if (!mvp_auto_mode_ || !mvp_network_ready_ || mvp_connecting_ || !protocol_ ||
        !protocol_->IsAudioChannelOpened() || GetDeviceState() != kDeviceStateIdle ||
        mvp_waiting_response_ || mvp_stopping_ || mvp_capture_started_ || mvp_receiving_audio_ ||
        mvp_playback_reset_pending_ || esp_timer_get_time() < mvp_auto_resume_at_us_ ||
        !audio_service_.IsPlaybackIdle() || !audio_service_.IsUplinkDrained()) {
        return;
    }
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    ArmDeviceVad();  // Also enforces the independent cloud error/quota deadline.
#else
    SetListeningMode(kListeningModeAutoStop);
#endif
}

void Application::HandleMvpPlaybackStop(const std::string& request_id) {
#if CONFIG_ENGLISH_MVP_DUPLEX
    if (mvp_duplex_active_) {
        mvp_waiting_response_ = false;
        mvp_stopping_ = false;
        if (request_id == mvp_playback_request_id_)
            mvp_playback_stop_received_ = true;
        MaybeConfirmMvpPlayback();
        return;  // Capture and its current turn survive output completion.
    }
#endif
    if (request_id == mvp_playback_request_id_) {
        mvp_playback_stop_received_ = true;
    }
    mvp_auto_resume_at_us_ = esp_timer_get_time() + 300000;
    mvp_waiting_response_ = false;
    mvp_stopping_ = false;
    mvp_capture_started_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    ResetDeviceVad();
#endif
    audio_service_.FinishRawCapture();
    SetDeviceState(kDeviceStateIdle);
    // STOP can arrive either before or after the final PCM output. Confirm only
    // after cleanup so immediate rearming cannot race this turn's input stop.
    MaybeConfirmMvpPlayback();
}

void Application::MaybeConfirmMvpPlayback() {
    if (mvp_playback_reset_pending_ || mvp_playback_request_id_.empty() ||
        !mvp_playback_started_ || !mvp_playback_stop_received_ ||
        !audio_service_.IsPlaybackIdle() || !protocol_ || mvp_connecting_ ||
        !protocol_->IsAudioChannelOpened()) {
        return;
    }
    if (protocol_->SendPlaybackCompleted(mvp_playback_request_id_)) {
        mvp_playback_request_id_.clear();
        mvp_playback_started_ = false;
        mvp_playback_stop_received_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        // Successful playback already reached the final PCM/queue drain. Resume
        // from this event (or the pending input-stop ACK), without a 300 ms delay
        // or the next 1 s network tick. Keep the input task's 120 ms protection.
        mvp_auto_resume_at_us_ = 0;
        MaybeResumeMvpAutoListening();
#endif
    }
}

void Application::InterruptMvpPlayback() {
    // The button is an immediate local stop. Never confirm queued audio that
    // was discarded by this decoder reset.
    mvp_playback_request_id_.clear();
    mvp_playback_started_ = false;
    mvp_playback_stop_received_ = false;
    mvp_receiving_audio_ = false;
    mvp_waiting_response_ = false;
    mvp_stopping_ = false;
    mvp_capture_started_ = false;
    mvp_auto_mode_ = false;
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    mvp_cloud_recovery_.Clear();
    ResetDeviceVad();
#endif
    audio_service_.ResetDecoder();
    AbortSpeaking(kAbortReasonNone);
    SetDeviceState(kDeviceStateIdle);
}
#endif

void Application::SetListeningMode(ListeningMode mode) {
    listening_mode_ = mode;
    SetDeviceState(kDeviceStateListening);
}

ListeningMode Application::GetDefaultListeningMode() const {
#if CONFIG_ENGLISH_MVP_LOCAL
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    return kListeningModeManualStop;
#else
    return mvp_auto_mode_ ? kListeningModeAutoStop : kListeningModeManualStop;
#endif
#else
    return aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime;
#endif
}

void Application::Reboot() {
#if CONFIG_ENGLISH_MVP_LOCAL
    // Do not destroy a socket while its background handshake is using it.
    esp_restart();
    return;
#endif
    ESP_LOGI(TAG, "Rebooting...");
    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }
    // Disconnect the audio channel
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        protocol_->CloseAudioChannel();
    }
    protocol_.reset();
    audio_service_.Stop();

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

bool Application::UpgradeFirmware(const std::string& url, const std::string& version) {
#if CONFIG_ENGLISH_MVP_LOCAL
    return false;
#endif
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();

    std::string upgrade_url = url;
    std::string version_info = version.empty() ? "(Manual upgrade)" : version;

    if (GetDeviceState() == kDeviceStateNotifying) {
        StopNotification();
    }

    // Close audio channel if it's open
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        ESP_LOGI(TAG, "Closing audio channel before firmware upgrade");
        protocol_->CloseAudioChannel();
    }
    ESP_LOGI(TAG, "Starting firmware upgrade from URL: %s", upgrade_url.c_str());

    Alert(Lang::Strings::OTA_UPGRADE, Lang::Strings::UPGRADING, "download",
          Lang::Sounds::OGG_UPGRADE);
    vTaskDelay(pdMS_TO_TICKS(3000));

    SetDeviceState(kDeviceStateUpgrading);

    std::string message = std::string(Lang::Strings::NEW_VERSION) + version_info;
    display->SetChatMessage("system", message.c_str());

    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.Stop();
    vTaskDelay(pdMS_TO_TICKS(1000));

    bool upgrade_success = Ota::Upgrade(upgrade_url, [this, display](int progress, size_t speed) {
        char buffer[32];
        snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
        Schedule([display, message = std::string(buffer)]() {
            display->SetChatMessage("system", message.c_str());
        });
    });

    if (!upgrade_success) {
        // Upgrade failed, restart audio service and continue running
        ESP_LOGE(TAG,
                 "Firmware upgrade failed, restarting audio service and continuing operation...");
        audio_service_.Start();                              // Restart audio service
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);  // Restore power save level
        Alert(Lang::Strings::ERROR, Lang::Strings::UPGRADE_FAILED, "cancel",
              Lang::Sounds::OGG_EXCLAMATION);
        vTaskDelay(pdMS_TO_TICKS(3000));
        return false;
    } else {
        // Upgrade success, reboot immediately
        ESP_LOGI(TAG, "Firmware upgrade successful, rebooting...");
        display->SetChatMessage("system", "Upgrade successful, rebooting...");
        vTaskDelay(pdMS_TO_TICKS(1000));  // Brief pause to show message
        Reboot();
        return true;
    }
}

void Application::WakeWordInvoke(const std::string& wake_word) {
#if CONFIG_ENGLISH_MVP_LOCAL
    ToggleChatState();
    return;
#endif
    if (!protocol_) {
        return;
    }

    auto state = GetDeviceState();

    if (state == kDeviceStateIdle) {
        // May be called from outside the main task (e.g. board button
        // callbacks), so schedule the invocation instead of running it here
        Schedule([this, wake_word]() {
            if (GetDeviceState() == kDeviceStateIdle) {
                BeginWakeWordInvoke(wake_word);
            }
        });
    } else if (state == kDeviceStateNotifying) {
        Schedule([this, wake_word]() {
            if (GetDeviceState() == kDeviceStateNotifying) {
                StopNotification();
                BeginWakeWordInvoke(wake_word);
            }
        });
    } else if (state == kDeviceStateSpeaking) {
        Schedule([this]() { AbortSpeaking(kAbortReasonNone); });
    } else if (state == kDeviceStateListening) {
        Schedule([this]() {
            if (protocol_) {
                protocol_->CloseAudioChannel();
            }
        });
    }
}

bool Application::CanEnterSleepMode() {
#if CONFIG_ENGLISH_MVP_LOCAL
    return false;  // Keep the local connection/retry timer active during bench testing.
#endif
    if (GetDeviceState() != kDeviceStateIdle) {
        return false;
    }

    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        return false;
    }

    if (!audio_service_.IsIdle()) {
        return false;
    }

    // Now it is safe to enter sleep mode
    return true;
}

void Application::RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback) {
    mcp_broadcast_callback_ = std::move(callback);
}

void Application::SendMcpMessage(const std::string& payload) {
#if CONFIG_ENGLISH_MVP_LOCAL && !CONFIG_ENGLISH_MVP_CLOUD
    return;
#endif
    // Always schedule to run in main task for thread safety
    Schedule([this, payload]() {
        if (protocol_) {
            protocol_->SendMcpMessage(payload);
        }
        if (mcp_broadcast_callback_) {
            mcp_broadcast_callback_(payload);
        }
    });
}

void Application::SetAecMode(AecMode mode) {
#if CONFIG_ENGLISH_MVP_LOCAL
    return;
#endif
    aec_mode_ = mode;
    Schedule([this]() {
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        switch (aec_mode_) {
            case kAecOff:
                audio_service_.EnableDeviceAec(false);
                display->ShowNotification(Lang::Strings::RTC_MODE_OFF);
                break;
            case kAecOnServerSide:
                audio_service_.EnableDeviceAec(false);
                display->ShowNotification(Lang::Strings::RTC_MODE_ON);
                break;
            case kAecOnDeviceSide:
                audio_service_.EnableDeviceAec(true);
                display->ShowNotification(Lang::Strings::RTC_MODE_ON);
                break;
        }

        // If the AEC mode is changed, close the audio channel
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
    });
}

void Application::PlaySound(const std::string_view& sound) { audio_service_.PlaySound(sound); }

void Application::ResetProtocol() {
    Schedule([this]() {
#if CONFIG_ENGLISH_MVP_LOCAL
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
        ResetDeviceVad();
        audio_service_.FinishRawCapture();
#endif
        mvp_network_ready_ = false;
        ++mvp_network_epoch_;
        mvp_want_recording_ = false;
        if (mvp_connecting_) {
            mvp_reset_pending_ = true;
            return;
        }
#endif
        if (GetDeviceState() == kDeviceStateNotifying) {
            StopNotification();
        }
        // Close audio channel if opened
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
        // Reset protocol
        protocol_.reset();
    });
}
