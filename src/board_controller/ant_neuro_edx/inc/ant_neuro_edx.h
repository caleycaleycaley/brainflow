#pragma once

#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "board.h"

#ifdef BUILD_ANT_EDX
#include <grpcpp/grpcpp.h>

#include "EdigRPC.grpc.pb.h"
#endif


class AntNeuroEdxBoard : public Board
{
private:
#ifdef BUILD_ANT_EDX
    struct EdxChannelMeta
    {
        int index;
        EdigRPC::gen::ChannelPolarity polarity;
        std::string name;
    };
#endif

    volatile bool keep_alive;
    bool initialized;
    bool is_streaming;
    std::thread streaming_thread;
    std::mutex wait_mutex;
    std::condition_variable wait_cv;
    volatile int state;
    int amplifier_handle;
    int requested_master_board;
    int package_num;
    int sampling_rate;
    double reference_range;
    double bipolar_range;
    bool impedance_mode;
    std::vector<int> active_channel_indices;
    std::vector<int> impedance_channel_rows;
    std::string endpoint;
    int trigger_channel_index;
    // Timing diagnostics are exposed via get_info and do not alter stream shape.
    uint64_t missing_start_frame_count;
    uint64_t fallback_timestamp_count;
    uint64_t non_monotonic_timestamp_count;
    uint64_t large_gap_count;
    double last_emitted_timestamp;
#ifdef BUILD_ANT_EDX
    std::shared_ptr<grpc::Channel> grpc_channel;
    std::unique_ptr<EdigRPC::gen::EdigRPC::Stub> stub;
    std::vector<EdxChannelMeta> channel_meta;
    std::vector<int> sampling_rates_available;
    std::vector<double> reference_ranges_available;
    std::vector<double> bipolar_ranges_available;
    std::vector<EdigRPC::gen::AmplifierMode> modes_available;
    std::string selected_model;
    std::string selected_device_key;
    std::string selected_device_serial;
#endif

    int validate_master_board ();
    int ensure_connected ();
    int set_mode ();
    int configure_stream_params (void *request_ptr);
    int process_frames ();
    int parse_edx_command (const std::string &config, std::string &response);
    void read_thread ();
    bool parse_bool_flag (const std::string &value, bool &flag);
#ifdef BUILD_ANT_EDX
    int connect_and_create_device ();
    int load_capabilities ();
    void rebuild_impedance_channel_rows ();
    int set_idle_mode ();
    int validate_sampling_rate (int value);
    int validate_reference_range (double value);
    int validate_bipolar_range (double value);
#endif

public:
    AntNeuroEdxBoard (struct BrainFlowInputParams params);
    ~AntNeuroEdxBoard ();

    int prepare_session () override;
    int start_stream (int buffer_size, const char *streamer_params) override;
    int stop_stream () override;
    int release_session () override;
    int config_board (std::string config, std::string &response) override;
};
