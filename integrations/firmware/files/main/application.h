#ifndef _APPLICATION_H_
#define _APPLICATION_H_

#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>

#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "audio_service.h"
#include "cloud_turn_recovery.h"
#include "device_state.h"
#include "device_state_machine.h"
#include "notify/notify_player.h"
#include "ota.h"
#include "protocol.h"

// Main event bits
#define MAIN_EVENT_SCHEDULE (1 << 0)
#define MAIN_EVENT_SEND_AUDIO (1 << 1)
#define MAIN_EVENT_WAKE_WORD_DETECTED (1 << 2)
#define MAIN_EVENT_VAD_CHANGE (1 << 3)
#define MAIN_EVENT_ERROR (1 << 4)
#define MAIN_EVENT_ACTIVATION_DONE (1 << 5)
#define MAIN_EVENT_CLOCK_TICK (1 << 6)
#define MAIN_EVENT_NETWORK_CONNECTED (1 << 7)
#define MAIN_EVENT_NETWORK_DISCONNECTED (1 << 8)
#define MAIN_EVENT_TOGGLE_CHAT (1 << 9)
#define MAIN_EVENT_START_LISTENING (1 << 10)
#define MAIN_EVENT_STOP_LISTENING (1 << 11)
#define MAIN_EVENT_STATE_CHANGED (1 << 12)
#define MAIN_EVENT_PLAYBACK_DRAINED (1 << 13)

enum AecMode {
    kAecOff,
    kAecOnDeviceSide,
    kAecOnServerSide,
};

class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    // Delete copy constructor and assignment operator
    Application(const Application&) = delete;
    Application& operator=(const Application&) = delete;

    /**
     * Initialize the application
     * This sets up display, audio, network callbacks, etc.
     * Network connection starts asynchronously.
     */
    void Initialize();

    /**
     * Run the main event loop
     * This function runs in the main task and never returns.
     * It handles all events including network, state changes, and user interactions.
     */
    void Run();

    DeviceState GetDeviceState() const { return state_machine_.GetState(); }
    bool IsVoiceDetected() const { return audio_service_.IsVoiceDetected(); }

    /**
     * Request state transition
     * Returns true if transition was successful
     */
    bool SetDeviceState(DeviceState state);

    /**
     * Schedule a callback to be executed in the main task
     */
    void Schedule(std::function<void()>&& callback);

    /**
     * Alert with status, message, emotion and optional sound
     */
    void Alert(const char* status, const char* message, const char* emotion = "",
               const std::string_view& sound = "");
    void DismissAlert();

    void AbortSpeaking(AbortReason reason);

    /**
     * Toggle chat state (event-based, thread-safe)
     * Sends MAIN_EVENT_TOGGLE_CHAT to be handled in Run()
     */
    void ToggleChatState();
#if CONFIG_ENGLISH_MVP_LOCAL
    void ToggleMvpAutoMode();
    bool SetRemoteMvpAutoMode(bool enabled);
    // Read-only UI snapshot; call from the main task like UpdateStatusBar().
    const char* GetMvpDisplayStatus() const;
    bool IsMvpAutoMode() const { return mvp_auto_mode_; }
    const char* GetMvpTeachingMode() const { return mvp_teaching_mode_; }
#endif

    /**
     * Start listening (event-based, thread-safe)
     * Sends MAIN_EVENT_START_LISTENING to be handled in Run()
     */
    void StartListening();

    /**
     * Stop listening (event-based, thread-safe)
     * Sends MAIN_EVENT_STOP_LISTENING to be handled in Run()
     */
    void StopListening();

    void Reboot();
    void WakeWordInvoke(const std::string& wake_word);
    bool UpgradeFirmware(const std::string& url, const std::string& version = "");
    bool CanEnterSleepMode();
    void SendMcpMessage(const std::string& payload);
    void RegisterMcpBroadcastCallback(std::function<void(const std::string&)> callback);
    void SetAecMode(AecMode mode);
    AecMode GetAecMode() const { return aec_mode_; }
    void PlaySound(const std::string_view& sound);
    AudioService& GetAudioService() { return audio_service_; }

    /**
     * Reset protocol resources (thread-safe)
     * Can be called from any task to release resources allocated after network connected
     * This includes closing audio channel, resetting protocol and ota objects
     */
    void ResetProtocol();

private:
    Application();
    ~Application();

    std::mutex mutex_;
    std::deque<std::function<void()>> main_tasks_;
    std::unique_ptr<Protocol> protocol_;
    EventGroupHandle_t event_group_ = nullptr;
    esp_timer_handle_t clock_timer_handle_ = nullptr;
    DeviceStateMachine state_machine_;
    ListeningMode listening_mode_ = kListeningModeAutoStop;
    AecMode aec_mode_ = kAecOff;
    std::string last_error_message_;
    AudioService audio_service_;
    NotifyPlayer notify_player_;
    uint32_t notification_playback_id_ = 0;
    std::unique_ptr<Ota> ota_;

    std::function<void(const std::string&)> mcp_broadcast_callback_;

    bool has_server_time_ = false;
    bool aborted_ = false;
    bool assets_version_checked_ = false;
    bool play_popup_on_listening_ =
        false;  // Flag to play popup sound after state changes to listening
    bool pending_listening_start_ =
        false;  // Waiting for playback to drain before starting listening (auto mode)
    int clock_ticks_ = 0;
    TaskHandle_t activation_task_handle_ = nullptr;
#if CONFIG_ENGLISH_MVP_LOCAL
    bool mvp_auto_mode_ = false;  // Boot in manual mode; double-click opts in.
    const char* mvp_teaching_mode_ = "unknown";  // Authoritative server state, main task only.
    void UpdateMvpTeachingMode(const char* mode);
    int64_t mvp_auto_resume_at_us_ = 0;
    bool mvp_stopping_ = false;
    bool mvp_waiting_response_ = false;
    bool mvp_capture_started_ = false;
    int64_t mvp_deadline_us_ = 0;
    void FinishMvpUpload();
    void MvpNetworkTick();
    void MaybeResumeMvpAutoListening();
    void StartMvpConnection();
    bool mvp_network_ready_ = false;
    bool mvp_connecting_ = false;
    bool mvp_want_recording_ = false;
    bool mvp_reset_pending_ = false;
    int mvp_retry_seconds_ = 1;
    int64_t mvp_retry_at_us_ = 0;
    int64_t mvp_ping_at_us_ = 0;
    uint32_t mvp_network_epoch_ = 0;
    uint32_t mvp_connect_epoch_ = 0;
    std::atomic<bool> mvp_receiving_audio_{false};
    std::atomic<bool> mvp_playback_reset_pending_{false};
    std::string mvp_playback_request_id_;
    bool mvp_playback_started_ = false;
    bool mvp_playback_stop_received_ = false;
#if CONFIG_ENGLISH_MVP_DUPLEX
    std::atomic<bool> mvp_duplex_active_{false};
    bool mvp_duplex_rejected_ = false;
    bool mvp_reference_fault_notified_ = false;
    void FinishEnvironmentTurn();
    void CancelEnvironmentPlayback();
#endif
    void HandleMvpPlaybackStop(const std::string& request_id);
    void MaybeConfirmMvpPlayback();
    void InterruptMvpPlayback();
#if CONFIG_ENGLISH_MVP_CLOUD && CONFIG_USE_AUDIO_PROCESSOR
    CloudTurnRecovery mvp_cloud_recovery_;
    bool mvp_vad_monitoring_ = false;
    bool mvp_vad_streaming_ = false;
    int64_t mvp_vad_silence_since_us_ = 0;
    std::deque<std::unique_ptr<AudioStreamPacket>> mvp_vad_preroll_;
    void ArmDeviceVad();
    void HandleDeviceVadChange(bool speaking);
    void MaybeFinishDeviceVadTurn();
    void ResetDeviceVad();
#endif
#endif

    // Event handlers
    void HandleStateChangedEvent();
    void HandleToggleChatEvent();
    void HandleStartListeningEvent();
    void HandleStopListeningEvent();
    void HandleNetworkConnectedEvent();
    void HandleNetworkDisconnectedEvent();
    void HandleActivationDoneEvent();
    void HandleWakeWordDetectedEvent();
    void ContinueOpenAudioChannel(ListeningMode mode);
    void BeginWakeWordInvoke(const std::string& wake_word);
    void ContinueWakeWordInvoke(const std::string& wake_word);
    void StartListeningAudio();
    void ConfigureWakeWordForListening();
    void StartNotification(std::string audio_url, std::vector<NotifySubtitle> subtitles);
    void StopNotification();
    void HandleNotificationFinished(uint32_t playback_id, bool success);

    // Activation task (runs in background)
    void ActivationTask();

    // Helper methods
    void CheckAssetsVersion();
    void CheckNewVersion();
    void InitializeProtocol();
    void ShowActivationCode(const std::string& code, const std::string& message);
    void SetListeningMode(ListeningMode mode);
    ListeningMode GetDefaultListeningMode() const;

    // State change handler called by state machine
    void OnStateChanged(DeviceState old_state, DeviceState new_state);
};

class TaskPriorityReset {
public:
    TaskPriorityReset(BaseType_t priority) {
        original_priority_ = uxTaskPriorityGet(NULL);
        vTaskPrioritySet(NULL, priority);
    }
    ~TaskPriorityReset() { vTaskPrioritySet(NULL, original_priority_); }

private:
    BaseType_t original_priority_;
};

#endif  // _APPLICATION_H_
