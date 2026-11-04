// Project-owned adapter. Build against a separately supplied InsMetaDataSDK.
#include <metaData.h>

#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;

static std::string quoted(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char ch : value) {
        if (ch == '"' || ch == '\\') out << '\\' << ch;
        else if (ch < 0x20) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << int(ch);
        else out << ch;
    }
    out << '"';
    return out.str();
}

static void number(std::ostream& out, double value) {
    if (std::isfinite(value)) out << std::setprecision(17) << value;
    else if (std::isnan(value)) out << "\"NaN\"";
    else out << (value > 0 ? "\"Infinity\"" : "\"-Infinity\"");
}

static void vector3(std::ostream& out, const double* values) {
    out << '[';
    for (int i = 0; i < 3; ++i) {
        if (i) out << ',';
        number(out, values[i]);
    }
    out << ']';
}

static void record(std::ostream& out, const GpsDataItem_t& item) {
    out << "{\"timestampMs\":" << item.timestampMs << ",\"latitude\":";
    number(out, item.latitude);
    out << ",\"north_south\":" << quoted(std::string(1, item.north_south)) << ",\"longitude\":";
    number(out, item.longitude);
    out << ",\"east_west\":" << quoted(std::string(1, item.east_west)) << ",\"altitude\":";
    number(out, item.altitude);
    out << '}';
}

static void record(std::ostream& out, const GyroDataItem_t& item) {
    out << "{\"timestamp\":" << item.timestamp << ",\"acceleration\":";
    vector3(out, item.acceleration);
    out << ",\"rotation\":";
    vector3(out, item.rotation);
    out << '}';
}

static void record(std::ostream& out, const ExposureDataItem_t& item) {
    out << "{\"timestamp\":" << item.timestamp << ",\"shutter_speed_s\":";
    number(out, item.shutter_speed_s);
    out << '}';
}

static void record(std::ostream& out, const RawTimelapseItem_t& item) {
    out << "{\"timestamp\":" << item.timestamp << '}';
}

template<class T, class Getter>
static std::string track(const fs::path& directory, const std::string& name, Getter get) {
    // Scope each vector separately: long recordings can contain millions of samples.
    std::vector<T> values;
    const bool available = get(values);
    const std::string filename = name + ".jsonl";
    std::ofstream out(directory / filename);
    out.exceptions(std::ios::badbit | std::ios::failbit);
    out.imbue(std::locale::classic());
    for (const auto& item : values) {
        record(out, item);
        out << '\n';
    }
    out.close();
    return "{\n"
           "            \"available\": " + std::string(available ? "true" : "false") + ",\n"
           "            \"count\": " + std::to_string(values.size()) + ",\n"
           "            \"file\": " + quoted(filename) + "\n"
           "        }";
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--schema") {
        std::cout << "{\"reader\":\"insta360-metadata\",\"schema_version\":1,"
                     "\"sdk\":\"InsMetaDataSDK\"}\n";
        return 0;
    }
    if (argc != 3) {
        std::cerr << "Usage: insta360-metadata INPUT EXISTING_EMPTY_OUTPUT_DIRECTORY\n";
        return 2;
    }
    try {
        const fs::path output(argv[2]);
        if (!fs::is_regular_file(argv[1]) || !fs::is_directory(output) || !fs::is_empty(output))
            throw std::runtime_error("Expected a regular input file and an empty output directory");
        ins_metadata::MetaDataParser parser;
        if (!parser.Parse(argv[1])) {
            std::cerr << "InsMetaDataSDK Parse failed\n";
            return 3;
        }
        const auto gps = track<GpsDataItem_t>(output, "gps", [&](auto& v) { return parser.GetGPSData(v); });
        const auto gyro = track<GyroDataItem_t>(output, "gyro", [&](auto& v) { return parser.GetGyroData(v); });
        const auto exposure = track<ExposureDataItem_t>(output, "exposure", [&](auto& v) { return parser.GetExposureData(v); });
        const auto timelapse = track<RawTimelapseItem_t>(output, "timelapse", [&](auto& v) { return parser.GetTimelapsePtsData(v); });
        std::ofstream summary(output / "sdk.json");
        summary.exceptions(std::ios::badbit | std::ios::failbit);
        summary << "{\n"
                   "    \"reader\": \"insta360-metadata\",\n"
                   "    \"schema_version\": 1,\n"
                   "    \"sdk\": \"InsMetaDataSDK\",\n"
                << "    \"camera_type\": " << quoted(parser.GetCameraType()) << ",\n"
                << "    \"firmware_version\": " << quoted(parser.GetFireWareVersion()) << ",\n"
                << "    \"serial_number\": " << quoted(parser.GetSerialNumber()) << ",\n"
                << "    \"gyro_first_timestamp_ms\": " << parser.GetFirstTimeStamp() << ",\n"
                << "    \"timestamp_policy\": \"SDK raw values; gyro origin is not a capture date; "
                   "SDK 2.0.2 GPS timestampMs name and seconds comment disagree\",\n"
                   "    \"tracks\": {\n"
                << "        \"gps\": " << gps << ",\n"
                << "        \"gyro\": " << gyro << ",\n"
                << "        \"exposure\": " << exposure << ",\n"
                << "        \"timelapse\": " << timelapse << "\n"
                   "    }\n"
                   "}\n";
        summary.close();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
