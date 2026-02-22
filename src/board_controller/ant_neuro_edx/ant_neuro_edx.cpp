#include "ant_neuro_edx.h"

#ifdef BUILD_ANT_EDX

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <regex>
#include <set>
#include <sstream>

#include "json.hpp"
#include "timestamp.h"

using json = nlohmann::json;

namespace
{
constexpr int edx_board_id = (int)BoardIds::ANT_NEURO_EDX_BOARD;

bool is_ant_master_board (int board_id)
{
    return ((board_id >= (int)BoardIds::ANT_NEURO_EE_410_BOARD &&
               board_id <= (int)BoardIds::ANT_NEURO_EE_225_BOARD) ||
        (board_id == (int)BoardIds::ANT_NEURO_EE_511_BOARD));
}

std::string to_upper (std::string s)
{
    std::transform (s.begin (), s.end (), s.begin (),
        [] (unsigned char c) { return (char)std::toupper (c); });
    return s;
}

std::vector<std::string> expected_tokens (int master_board)
{
    switch ((BoardIds)master_board)
    {
        case BoardIds::ANT_NEURO_EE_511_BOARD:
            return {"EE-511", "EE-5XX"};
        case BoardIds::ANT_NEURO_EE_410_BOARD:
        case BoardIds::ANT_NEURO_EE_411_BOARD:
        case BoardIds::ANT_NEURO_EE_430_BOARD:
            return {"EE-4XX"};
        default:
            return {"EE-2XX"};
    }
}

std::set<std::string> extract_tokens (const std::string &value)
{
    std::set<std::string> result;
    std::regex re ("EE[\\-_ ]?([245][0-9X]{2})");
    std::string upper = to_upper (value);
    auto begin = std::sregex_iterator (upper.begin (), upper.end (), re);
    auto end = std::sregex_iterator ();
    for (auto it = begin; it != end; ++it)
    {
        std::string suffix = (*it)[1].str ();
        std::string token = "EE-" + suffix;
        result.insert (token);
        if (suffix.size () == 3 && std::isdigit ((unsigned char)suffix[0]))
        {
            result.insert (std::string ("EE-") + suffix[0] + "XX");
        }
    }
    return result;
}

bool has_match (const std::set<std::string> &tokens, const std::vector<std::string> &need)
{
    for (const auto &token : need)
    {
        if (tokens.find (token) != tokens.end ())
        {
            return true;
        }
    }
    return false;
}

double ts_to_unix (const google::protobuf::Timestamp &ts)
{
    return (double)ts.seconds () + ((double)ts.nanos () / 1000000000.0);
}

int map_status (const grpc::Status &status)
{
    if (status.ok ())
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    if (status.error_code () == grpc::StatusCode::DEADLINE_EXCEEDED)
    {
        return (int)BrainFlowExitCodes::SYNC_TIMEOUT_ERROR;
    }
    if (status.error_code () == grpc::StatusCode::INVALID_ARGUMENT)
    {
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }
    return (int)BrainFlowExitCodes::GENERAL_ERROR;
}

int handshake_status_to_brainflow (const grpc::Status &status)
{
    if (status.ok ())
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }

    if (status.error_code () == grpc::StatusCode::UNIMPLEMENTED)
    {
        return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
    }
    if (status.error_code () == grpc::StatusCode::UNAVAILABLE)
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }
    if (status.error_code () == grpc::StatusCode::DEADLINE_EXCEEDED)
    {
        return (int)BrainFlowExitCodes::SYNC_TIMEOUT_ERROR;
    }
    return map_status (status);
}

std::vector<int> try_get_vec (const json &obj, const char *key)
{
    try
    {
        return obj[key].get<std::vector<int>> ();
    }
    catch (...)
    {
        return {};
    }
}
} // namespace

AntNeuroEdxBoard::AntNeuroEdxBoard (struct BrainFlowInputParams params) : Board (edx_board_id, params)
{
    keep_alive = false;
    initialized = false;
    is_streaming = false;
    state = (int)BrainFlowExitCodes::SYNC_TIMEOUT_ERROR;
    amplifier_handle = -1;
    requested_master_board = params.master_board;
    package_num = 0;
    impedance_mode = false;
    sampling_rate = -1;
    reference_range = -1.0;
    bipolar_range = -1.0;
    trigger_channel_index = -1;
    missing_start_frame_count = 0;
    fallback_timestamp_count = 0;
    non_monotonic_timestamp_count = 0;
    large_gap_count = 0;
    last_emitted_timestamp = -1.0;
}

AntNeuroEdxBoard::~AntNeuroEdxBoard ()
{
    skip_logs = true;
    release_session ();
}

int AntNeuroEdxBoard::validate_master_board ()
{
    if (!is_ant_master_board (requested_master_board))
    {
        safe_logger (spdlog::level::err, "invalid master_board for EDX: {}", requested_master_board);
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::ensure_connected ()
{
    if (!params.other_info.empty () && params.ip_address.empty () && (params.ip_port <= 0))
    {
        safe_logger (spdlog::level::err,
            "EDX endpoint in other_info is no longer supported for board 66, use ip_address/ip_port");
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    if (params.ip_address.empty ())
    {
        safe_logger (spdlog::level::err, "EDX requires ip_address");
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    if ((params.ip_port <= 0) || (params.ip_port > 65535))
    {
        safe_logger (spdlog::level::err, "EDX requires valid ip_port (1..65535), got {}", params.ip_port);
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    if (!params.other_info.empty ())
    {
        safe_logger (spdlog::level::warn,
            "EDX endpoint in other_info is no longer supported for board 66, use ip_address/ip_port");
    }

    if ((params.ip_address.find ("://") != std::string::npos) ||
        (params.ip_address.find ("/") != std::string::npos))
    {
        safe_logger (spdlog::level::err,
            "EDX requires host-only ip_address, got URI-like value '{}'",
            params.ip_address);
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    endpoint = "dns:///" + params.ip_address + ":" + std::to_string (params.ip_port);

    grpc_channel = grpc::CreateChannel (endpoint, grpc::InsecureChannelCredentials ());
    stub = EdigRPC::gen::EdigRPC::NewStub (grpc_channel);
    return stub ? (int)BrainFlowExitCodes::STATUS_OK : (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
}

int AntNeuroEdxBoard::connect_and_create_device ()
{
    EdigRPC::gen::DeviceManager_GetDevicesRequest req;
    EdigRPC::gen::DeviceManager_GetDevicesResponse resp;
    grpc::ClientContext ctx;
    ctx.set_deadline (
        std::chrono::system_clock::now () + std::chrono::seconds (std::max (1, params.timeout)));
    grpc::Status status = stub->DeviceManager_GetDevices (&ctx, req, &resp);
    if (!status.ok ())
    {
        return map_status (status);
    }

    std::vector<std::string> need = expected_tokens (requested_master_board);
    std::vector<EdigRPC::gen::DeviceInfo> matched;
    std::vector<EdigRPC::gen::DeviceInfo> serial_matched;

    for (const auto &info : resp.deviceinfolist ())
    {
        std::set<std::string> tokens = extract_tokens (info.key () + " " + info.serial ());
        if (!has_match (tokens, need))
        {
            continue;
        }
        matched.push_back (info);
        if (!params.serial_number.empty ())
        {
            std::string serial_u = to_upper (params.serial_number);
            if (to_upper (info.key ()).find (serial_u) != std::string::npos ||
                to_upper (info.serial ()).find (serial_u) != std::string::npos)
            {
                serial_matched.push_back (info);
            }
        }
    }

    if (matched.empty () || (!params.serial_number.empty () && serial_matched.empty ()))
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }

    EdigRPC::gen::DeviceInfo selected =
        (!params.serial_number.empty ()) ? serial_matched.front () : matched.front ();
    selected_device_key = selected.key ();
    selected_device_serial = selected.serial ();
    std::set<std::string> tokens = extract_tokens (selected_device_key);
    if (!tokens.empty ())
    {
        selected_model = *tokens.begin ();
    }

    EdigRPC::gen::Controller_CreateDeviceRequest create_req;
    *create_req.add_deviceinfolist () = selected;
    EdigRPC::gen::Controller_CreateDeviceResponse create_resp;
    grpc::ClientContext create_ctx;
    create_ctx.set_deadline (
        std::chrono::system_clock::now () + std::chrono::seconds (std::max (1, params.timeout)));
    status = stub->Controller_CreateDevice (&create_ctx, create_req, &create_resp);
    if (!status.ok ())
    {
        return map_status (status);
    }

    amplifier_handle = create_resp.amplifierhandle ();
    return (amplifier_handle < 0) ? (int)BrainFlowExitCodes::GENERAL_ERROR :
                                    (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::load_capabilities ()
{
    EdigRPC::gen::Amplifier_GetChannelsAvailableRequest channels_req;
    channels_req.set_amplifierhandle (amplifier_handle);
    EdigRPC::gen::Amplifier_GetChannelsAvailableResponse channels_resp;
    grpc::ClientContext channels_ctx;
    channels_ctx.set_deadline (std::chrono::system_clock::now () +
        std::chrono::seconds (std::max (1, params.timeout)));
    grpc::Status status = stub->Amplifier_GetChannelsAvailable (&channels_ctx, channels_req, &channels_resp);
    if (!status.ok ())
    {
        return map_status (status);
    }

    channel_meta.clear ();
    active_channel_indices.clear ();
    trigger_channel_index = -1;
    for (const auto &channel : channels_resp.channellist ())
    {
        EdxChannelMeta meta;
        meta.index = channel.channelindex ();
        meta.polarity = channel.channelpolarity ();
        meta.name = channel.name ();
        channel_meta.push_back (meta);
        if (meta.polarity == EdigRPC::gen::ChannelPolarity::Referential ||
            meta.polarity == EdigRPC::gen::ChannelPolarity::Bipolar)
        {
            active_channel_indices.push_back (meta.index);
        }
        if (to_upper (meta.name).find ("TRIGGER") != std::string::npos ||
            meta.polarity == EdigRPC::gen::ChannelPolarity::Receiver ||
            meta.polarity == EdigRPC::gen::ChannelPolarity::Transmitter)
        {
            trigger_channel_index = meta.index;
        }
    }

    EdigRPC::gen::Amplifier_GetSamplingRatesAvailableRequest rates_req;
    rates_req.set_amplifierhandle (amplifier_handle);
    EdigRPC::gen::Amplifier_GetSamplingRatesAvailableResponse rates_resp;
    grpc::ClientContext rates_ctx;
    rates_ctx.set_deadline (std::chrono::system_clock::now () +
        std::chrono::seconds (std::max (1, params.timeout)));
    status = stub->Amplifier_GetSamplingRatesAvailable (&rates_ctx, rates_req, &rates_resp);
    if (!status.ok ())
    {
        return map_status (status);
    }
    sampling_rates_available.clear ();
    for (double rate : rates_resp.ratelist ())
    {
        sampling_rates_available.push_back ((int)std::lround (rate));
    }
    if (!sampling_rates_available.empty ())
    {
        sampling_rate = sampling_rates_available.front ();
    }

    EdigRPC::gen::Amplifier_GetRangesAvailableRequest ranges_req;
    ranges_req.set_amplifierhandle (amplifier_handle);
    EdigRPC::gen::Amplifier_GetRangesAvailableResponse ranges_resp;
    grpc::ClientContext ranges_ctx;
    ranges_ctx.set_deadline (std::chrono::system_clock::now () +
        std::chrono::seconds (std::max (1, params.timeout)));
    status = stub->Amplifier_GetRangesAvailable (&ranges_ctx, ranges_req, &ranges_resp);
    if (!status.ok ())
    {
        return map_status (status);
    }
    reference_ranges_available.clear ();
    bipolar_ranges_available.clear ();
    for (const auto &entry : ranges_resp.rangemap ())
    {
        if (entry.first == (int)EdigRPC::gen::ChannelPolarity::Referential)
        {
            reference_ranges_available.assign (entry.second.values ().begin (), entry.second.values ().end ());
        }
        else if (entry.first == (int)EdigRPC::gen::ChannelPolarity::Bipolar)
        {
            bipolar_ranges_available.assign (entry.second.values ().begin (), entry.second.values ().end ());
        }
    }

    EdigRPC::gen::Amplifier_GetModesAvailableRequest modes_req;
    modes_req.set_amplifierhandle (amplifier_handle);
    EdigRPC::gen::Amplifier_GetModesAvailableResponse modes_resp;
    grpc::ClientContext modes_ctx;
    modes_ctx.set_deadline (std::chrono::system_clock::now () +
        std::chrono::seconds (std::max (1, params.timeout)));
    status = stub->Amplifier_GetModesAvailable (&modes_ctx, modes_req, &modes_resp);
    if (!status.ok ())
    {
        return map_status (status);
    }
    modes_available.clear ();
    for (auto mode : modes_resp.modelist ())
    {
        modes_available.push_back ((EdigRPC::gen::AmplifierMode)mode);
    }

    if (reference_range <= 0.0 && !reference_ranges_available.empty ())
    {
        reference_range = reference_ranges_available.front ();
    }
    if (bipolar_range <= 0.0 && !bipolar_ranges_available.empty ())
    {
        bipolar_range = bipolar_ranges_available.front ();
    }
    if (selected_model == "EE-511" || to_upper (selected_device_serial).find ("EE511") != std::string::npos)
    {
        reference_range = 1.0;
        bipolar_range = 2.5;
    }

    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::prepare_session ()
{
    if (initialized)
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }

    int res = validate_master_board ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }
    res = ensure_connected ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }

    // Fail fast on incompatible gRPC service contract before device/session flow.
    {
        EdigRPC::gen::GetStateRequest state_req;
        EdigRPC::gen::GetStateResponse state_resp;
        grpc::ClientContext state_ctx;
        state_ctx.set_deadline (std::chrono::system_clock::now () +
            std::chrono::seconds (std::max (1, params.timeout)));
        grpc::Status state_status = stub->GetState (&state_ctx, state_req, &state_resp);
        if (!state_status.ok ())
        {
            safe_logger (spdlog::level::err,
                "EDX handshake failed: GetState RPC not usable (grpc_code={}, message='{}'). "
                "Likely client/server proto contract mismatch or endpoint misconfiguration.",
                (int)state_status.error_code (), state_status.error_message ());
            return handshake_status_to_brainflow (state_status);
        }
        if (state_resp.id ().empty ())
        {
            safe_logger (spdlog::level::err,
                "EDX handshake failed: GetState returned empty Id, refusing to continue.");
            return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
        }
    }

    try
    {
        board_descr =
            boards_struct.brainflow_boards_json["boards"][std::to_string (requested_master_board)];
        sampling_rate = board_descr["default"]["sampling_rate"];
    }
    catch (...)
    {
        return (int)BrainFlowExitCodes::GENERAL_ERROR;
    }

    res = connect_and_create_device ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }
    res = load_capabilities ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        release_session ();
        return res;
    }

    initialized = true;
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::configure_stream_params (void *request_ptr)
{
    EdigRPC::gen::Amplifier_SetModeRequest *request =
        static_cast<EdigRPC::gen::Amplifier_SetModeRequest *> (request_ptr);
    auto *stream_params = request->mutable_streamparams ();
    stream_params->set_samplingrate (sampling_rate);
    stream_params->set_datareadypercentage (5);
    stream_params->set_buffersize (1024);
    stream_params->clear_ranges ();
    for (int channel_index : active_channel_indices)
    {
        stream_params->add_activechannels (channel_index);
    }
    if (reference_range > 0.0)
    {
        (*stream_params->mutable_ranges ())[(int)EdigRPC::gen::ChannelPolarity::Referential] =
            reference_range;
    }
    if (bipolar_range > 0.0)
    {
        (*stream_params->mutable_ranges ())[(int)EdigRPC::gen::ChannelPolarity::Bipolar] =
            bipolar_range;
    }
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::set_mode ()
{
    EdigRPC::gen::Amplifier_SetModeRequest request;
    request.set_amplifierhandle (amplifier_handle);
    request.set_mode (
        impedance_mode ? EdigRPC::gen::AmplifierMode::AmplifierMode_Impedance :
                         EdigRPC::gen::AmplifierMode::AmplifierMode_Eeg);
    configure_stream_params (&request);

    EdigRPC::gen::Amplifier_SetModeResponse response;
    grpc::ClientContext ctx;
    ctx.set_deadline (
        std::chrono::system_clock::now () + std::chrono::seconds (std::max (1, params.timeout)));
    grpc::Status status = stub->Amplifier_SetMode (&ctx, request, &response);
    return map_status (status);
}

int AntNeuroEdxBoard::set_idle_mode ()
{
    if (!stub || amplifier_handle < 0)
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }

    EdigRPC::gen::Amplifier_SetModeRequest request;
    request.set_amplifierhandle (amplifier_handle);
    request.set_mode (EdigRPC::gen::AmplifierMode::AmplifierMode_Idle);
    EdigRPC::gen::Amplifier_SetModeResponse response;

    grpc::ClientContext ctx;
    ctx.set_deadline (
        std::chrono::system_clock::now () + std::chrono::seconds (std::max (1, params.timeout)));
    grpc::Status status = stub->Amplifier_SetMode (&ctx, request, &response);
    return map_status (status);
}

void AntNeuroEdxBoard::read_thread ()
{
    int sleep_time_ms = 5;
    int wait_attempts = 0;
    int max_wait_attempts = std::max (1, params.timeout) * 1000 / sleep_time_ms;

    while (keep_alive)
    {
        int res = process_frames ();
        if (res == (int)BrainFlowExitCodes::STATUS_OK)
        {
            if (state != (int)BrainFlowExitCodes::STATUS_OK)
            {
                {
                    std::lock_guard<std::mutex> lk (wait_mutex);
                    state = (int)BrainFlowExitCodes::STATUS_OK;
                }
                wait_cv.notify_one ();
            }
            wait_attempts = 0;
        }
        else
        {
            wait_attempts++;
            if (wait_attempts >= max_wait_attempts)
            {
                {
                    std::lock_guard<std::mutex> lk (wait_mutex);
                    state = res;
                }
                wait_cv.notify_one ();
                break;
            }
            std::this_thread::sleep_for (std::chrono::milliseconds (sleep_time_ms));
        }
    }
}

int AntNeuroEdxBoard::process_frames ()
{
    EdigRPC::gen::Amplifier_GetFrameRequest request;
    request.set_amplifierhandle (amplifier_handle);
    EdigRPC::gen::Amplifier_GetFrameResponse response;

    grpc::ClientContext ctx;
    ctx.set_deadline (std::chrono::system_clock::now () + std::chrono::milliseconds (500));
    grpc::Status status = stub->Amplifier_GetFrame (&ctx, request, &response);
    if (!status.ok ())
    {
        return map_status (status);
    }
    if (response.framelist ().empty ())
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }

    int num_rows = board_descr["default"]["num_rows"];
    int package_num_channel = board_descr["default"]["package_num_channel"];
    int timestamp_channel = board_descr["default"]["timestamp_channel"];
    int marker_channel = board_descr["default"]["marker_channel"];
    std::vector<int> eeg_channels = try_get_vec (board_descr["default"], "eeg_channels");
    std::vector<int> emg_channels = try_get_vec (board_descr["default"], "emg_channels");
    std::vector<int> resistance_channels = try_get_vec (board_descr["default"], "resistance_channels");
    std::vector<int> other_channels = try_get_vec (board_descr["default"], "other_channels");
    std::vector<double> package ((size_t)num_rows, 0.0);
    const double sample_dt = (sampling_rate > 0) ? (1.0 / (double)sampling_rate) : 0.0;
    const double large_gap_threshold = 1.0;

    for (const auto &frame : response.framelist ())
    {
        const bool has_start = frame.has_start ();
        const double frame_base_ts = has_start ? ts_to_unix (frame.start ()) : get_timestamp ();
        if (!has_start)
        {
            missing_start_frame_count++;
        }

        bool impedance_frame =
            impedance_mode || frame.frametype () == EdigRPC::gen::AmplifierFrameType::AmplifierFrameType_ImpedanceVoltages;
        if (impedance_frame)
        {
            std::fill (package.begin (), package.end (), 0.0);
            int idx = 0;
            for (const auto &entry : frame.impedance ().channels ())
            {
                if (idx >= (int)resistance_channels.size ())
                {
                    break;
                }
                package[(size_t)resistance_channels[(size_t)idx++]] = (double)entry.value ();
            }
            package[(size_t)package_num_channel] = (double)package_num++;
            package[(size_t)timestamp_channel] = frame_base_ts;
            if (!has_start)
            {
                fallback_timestamp_count++;
            }
            if (last_emitted_timestamp > 0.0)
            {
                if (package[(size_t)timestamp_channel] < last_emitted_timestamp)
                {
                    non_monotonic_timestamp_count++;
                }
                if ((package[(size_t)timestamp_channel] - last_emitted_timestamp) > large_gap_threshold)
                {
                    large_gap_count++;
                }
            }
            last_emitted_timestamp = package[(size_t)timestamp_channel];
            push_package (package.data ());
            continue;
        }

        int cols = frame.matrix ().cols ();
        int rows = frame.matrix ().rows ();
        if (cols <= 0 || rows <= 0 || cols * rows != frame.matrix ().data_size ())
        {
            return (int)BrainFlowExitCodes::GENERAL_ERROR;
        }

        for (int row = 0; row < rows; row++)
        {
            std::fill (package.begin (), package.end (), 0.0);
            int eeg_counter = 0;
            int emg_counter = 0;
            for (int col = 0; col < cols; col++)
            {
                double value = frame.matrix ().data (row * cols + col);
                int channel_index = (col < (int)active_channel_indices.size ()) ?
                        active_channel_indices[(size_t)col] :
                        col;

                EdigRPC::gen::ChannelPolarity polarity = EdigRPC::gen::ChannelPolarity::Referential;
                for (const auto &meta : channel_meta)
                {
                    if (meta.index == channel_index)
                    {
                        polarity = meta.polarity;
                        break;
                    }
                }

                if (polarity == EdigRPC::gen::ChannelPolarity::Referential &&
                    eeg_counter < (int)eeg_channels.size ())
                {
                    package[(size_t)eeg_channels[(size_t)eeg_counter++]] = value;
                }
                else if (polarity == EdigRPC::gen::ChannelPolarity::Bipolar &&
                    emg_counter < (int)emg_channels.size ())
                {
                    package[(size_t)emg_channels[(size_t)emg_counter++]] = value;
                }
                if (channel_index == trigger_channel_index && !other_channels.empty ())
                {
                    package[(size_t)other_channels[0]] = value;
                }
            }
            package[(size_t)package_num_channel] = (double)package_num++;
            package[(size_t)timestamp_channel] = frame_base_ts + (double)row * sample_dt;
            if (!has_start)
            {
                fallback_timestamp_count++;
            }
            if (last_emitted_timestamp > 0.0)
            {
                if (package[(size_t)timestamp_channel] < last_emitted_timestamp)
                {
                    non_monotonic_timestamp_count++;
                }
                if ((package[(size_t)timestamp_channel] - last_emitted_timestamp) > large_gap_threshold)
                {
                    large_gap_count++;
                }
            }
            last_emitted_timestamp = package[(size_t)timestamp_channel];
            if (frame.timemarkers_size () > 0)
            {
                package[(size_t)marker_channel] = (double)frame.timemarkers (0).timemarkercode ();
            }
            push_package (package.data ());
        }
    }

    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::start_stream (int buffer_size, const char *streamer_params)
{
    if (!initialized)
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }
    if (is_streaming)
    {
        return (int)BrainFlowExitCodes::STREAM_ALREADY_RUN_ERROR;
    }

    int res = prepare_for_acquisition (buffer_size, streamer_params);
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }

    res = set_mode ();
    if (res != (int)BrainFlowExitCodes::STATUS_OK)
    {
        return res;
    }

    last_emitted_timestamp = -1.0;
    non_monotonic_timestamp_count = 0;
    large_gap_count = 0;
    fallback_timestamp_count = 0;
    missing_start_frame_count = 0;
    keep_alive = true;
    state = (int)BrainFlowExitCodes::SYNC_TIMEOUT_ERROR;
    streaming_thread = std::thread ([this] { read_thread (); });

    std::unique_lock<std::mutex> lk (wait_mutex);
    auto sec = std::chrono::seconds (std::max (1, params.timeout));
    if (wait_cv.wait_for (
            lk, sec, [this] { return state != (int)BrainFlowExitCodes::SYNC_TIMEOUT_ERROR; }))
    {
        if (state == (int)BrainFlowExitCodes::STATUS_OK)
        {
            is_streaming = true;
        }
        return state;
    }

    keep_alive = false;
    if (streaming_thread.joinable ())
    {
        streaming_thread.join ();
    }
    free_packages ();
    return (int)BrainFlowExitCodes::SYNC_TIMEOUT_ERROR;
}

int AntNeuroEdxBoard::stop_stream ()
{
    if (!is_streaming && !keep_alive)
    {
        return (int)BrainFlowExitCodes::STREAM_THREAD_IS_NOT_RUNNING;
    }

    keep_alive = false;
    is_streaming = false;
    if (streaming_thread.joinable ())
    {
        streaming_thread.join ();
    }
    set_idle_mode ();
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::release_session ()
{
    if (is_streaming || keep_alive)
    {
        stop_stream ();
    }

    set_idle_mode ();
    if (stub && amplifier_handle >= 0)
    {
        EdigRPC::gen::Amplifier_DisposeRequest request;
        request.set_amplifierhandle (amplifier_handle);
        EdigRPC::gen::Amplifier_DisposeResponse response;
        grpc::ClientContext ctx;
        ctx.set_deadline (std::chrono::system_clock::now () +
            std::chrono::seconds (std::max (1, params.timeout)));
        stub->Amplifier_Dispose (&ctx, request, &response);
    }

    free_packages ();
    initialized = false;
    amplifier_handle = -1;
    stub.reset ();
    grpc_channel.reset ();
    return (int)BrainFlowExitCodes::STATUS_OK;
}

bool AntNeuroEdxBoard::parse_bool_flag (const std::string &value, bool &flag)
{
    if (value == "0")
    {
        flag = false;
        return true;
    }
    if (value == "1")
    {
        flag = true;
        return true;
    }
    return false;
}

int AntNeuroEdxBoard::validate_sampling_rate (int value)
{
    if (sampling_rates_available.empty ())
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    return (std::find (sampling_rates_available.begin (), sampling_rates_available.end (), value) !=
               sampling_rates_available.end ()) ?
        (int)BrainFlowExitCodes::STATUS_OK :
        (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
}

int AntNeuroEdxBoard::validate_reference_range (double value)
{
    if (reference_ranges_available.empty ())
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    return (std::find (reference_ranges_available.begin (), reference_ranges_available.end (), value) !=
               reference_ranges_available.end ()) ?
        (int)BrainFlowExitCodes::STATUS_OK :
        (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
}

int AntNeuroEdxBoard::validate_bipolar_range (double value)
{
    if (bipolar_ranges_available.empty ())
    {
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    return (std::find (bipolar_ranges_available.begin (), bipolar_ranges_available.end (), value) !=
               bipolar_ranges_available.end ()) ?
        (int)BrainFlowExitCodes::STATUS_OK :
        (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
}

int AntNeuroEdxBoard::parse_edx_command (const std::string &config, std::string &response)
{
    std::vector<std::string> parts;
    std::stringstream ss (config);
    std::string token;
    while (std::getline (ss, token, ':'))
    {
        parts.push_back (token);
    }
    if (parts.size () < 2 || parts[0] != "edx")
    {
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    json out;
    out["op"] = parts[1];
    if (parts[1] == "get_capabilities")
    {
        out["sampling_rates"] = sampling_rates_available;
        out["active_channels"] = active_channel_indices;
        out["selected_model"] = selected_model;
    }
    else if (parts[1] == "get_mode")
    {
        EdigRPC::gen::Amplifier_GetModeRequest request;
        request.set_amplifierhandle (amplifier_handle);
        EdigRPC::gen::Amplifier_GetModeResponse mode_response;
        grpc::ClientContext ctx;
        ctx.set_deadline (std::chrono::system_clock::now () +
            std::chrono::seconds (std::max (1, params.timeout)));
        grpc::Status status = stub->Amplifier_GetMode (&ctx, request, &mode_response);
        if (!status.ok ())
        {
            return map_status (status);
        }
        std::vector<int> modes;
        for (auto mode : mode_response.modelist ())
        {
            modes.push_back ((int)mode);
        }
        out["mode_list"] = modes;
    }
    else if (parts[1] == "get_power")
    {
        EdigRPC::gen::Amplifier_GetPowerRequest request;
        request.set_amplifierhandle (amplifier_handle);
        EdigRPC::gen::Amplifier_GetPowerResponse power_response;
        grpc::ClientContext ctx;
        ctx.set_deadline (std::chrono::system_clock::now () +
            std::chrono::seconds (std::max (1, params.timeout)));
        grpc::Status status = stub->Amplifier_GetPower (&ctx, request, &power_response);
        if (!status.ok ())
        {
            return map_status (status);
        }
        if (power_response.powerlist_size () > 0)
        {
            out["battery_level"] = power_response.powerlist (0).batterylevel ();
        }
    }
    else
    {
        return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
    }

    out["status"] = "ok";
    response = out.dump ();
    return (int)BrainFlowExitCodes::STATUS_OK;
}

int AntNeuroEdxBoard::config_board (std::string config, std::string &response)
{
    if (!initialized)
    {
        return (int)BrainFlowExitCodes::BOARD_NOT_READY_ERROR;
    }

    if (config.rfind ("edx:", 0) == 0)
    {
        return parse_edx_command (config, response);
    }
    if (config.find ("sampling_rate:") == 0)
    {
        int value = std::stoi (config.substr (14));
        int res = validate_sampling_rate (value);
        if (res != (int)BrainFlowExitCodes::STATUS_OK)
        {
            return res;
        }
        sampling_rate = value;
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    if (config.find ("reference_range:") == 0)
    {
        reference_range = std::stod (config.substr (16));
        int res = validate_reference_range (reference_range);
        if (res != (int)BrainFlowExitCodes::STATUS_OK)
        {
            return res;
        }
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    if (config.find ("bipolar_range:") == 0)
    {
        bipolar_range = std::stod (config.substr (14));
        int res = validate_bipolar_range (bipolar_range);
        if (res != (int)BrainFlowExitCodes::STATUS_OK)
        {
            return res;
        }
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    if (config.find ("impedance_mode:") == 0)
    {
        bool mode = false;
        if (!parse_bool_flag (config.substr (15), mode))
        {
            return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
        }
        impedance_mode = mode;
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    if (config == "get_info")
    {
        json info;
        info["endpoint"] = endpoint;
        info["master_board"] = requested_master_board;
        info["sampling_rate"] = sampling_rate;
        info["selected_model"] = selected_model;
        info["selected_key"] = selected_device_key;
        info["selected_serial"] = selected_device_serial;
        info["timing"] = {
            {"missing_start_frame_count", missing_start_frame_count},
            {"fallback_timestamp_count", fallback_timestamp_count},
            {"non_monotonic_timestamp_count", non_monotonic_timestamp_count},
            {"large_gap_count", large_gap_count},
            {"last_emitted_timestamp", last_emitted_timestamp}};
        response = info.dump ();
        return (int)BrainFlowExitCodes::STATUS_OK;
    }
    return (int)BrainFlowExitCodes::INVALID_ARGUMENTS_ERROR;
}

#else

#include "brainflow_constants.h"

AntNeuroEdxBoard::AntNeuroEdxBoard (struct BrainFlowInputParams params)
    : Board ((int)BoardIds::ANT_NEURO_EDX_BOARD, params)
{
}

AntNeuroEdxBoard::~AntNeuroEdxBoard ()
{
}

int AntNeuroEdxBoard::prepare_session ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::start_stream (int, const char *)
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::stop_stream ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::release_session ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::config_board (std::string, std::string &)
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::validate_master_board ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::ensure_connected ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::set_mode ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::configure_stream_params (void *)
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::process_frames ()
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

int AntNeuroEdxBoard::parse_edx_command (const std::string &, std::string &)
{
    return (int)BrainFlowExitCodes::UNSUPPORTED_BOARD_ERROR;
}

void AntNeuroEdxBoard::read_thread ()
{
}

bool AntNeuroEdxBoard::parse_bool_flag (const std::string &, bool &)
{
    return false;
}

#endif
