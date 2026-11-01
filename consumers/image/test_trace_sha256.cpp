#include "trace_sha256.hpp"
#include <cstdio>
#include <string>
#include <vector>

int main() {
    struct Case { size_t size; const char *sha; };
    const Case cases[] = {
        {0,"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
        {1,"6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"},
        {55,"463eb28e72f82e0a96c0a4cc53690c571281131f672aa229e0d45ae59b598b59"},
        {56,"da2ae4d6b36748f2a318f23e7ab1dfdf45acdc9d049bd80e59de82a60895f562"},
        {63,"29af2686fd53374a36b0846694cc342177e428d1647515f078784d69cdb9e488"},
        {64,"fdeab9acf3710362bd2658cdc9a29e8f9c757fcf9811603a8c447cd1d9151108"},
        {65,"4bfd2c8b6f1eec7a2afeb48b934ee4b2694182027e6d0fc075074f2fabb31781"},
        {255,"857df204175f077a9986709897f00ee0bcc0449585248e4b42498337e9329999"},
        {1024,"2bce1ba628720664be4b9fdd77aae0678e5f0f3f02fc6ff641ec879094f6a404"},
        {3u*8u*1024u*1024u,"cd02b02a6b19e0c10dcc9203daae0f981d53e02570c457af704eedd8b6abbdcb"}
    };
    for (const auto &item : cases) {
        std::vector<unsigned char> value(item.size);
        for (size_t i = 0; i < value.size(); ++i) value[i] = static_cast<unsigned char>(i % 251);
        if (image_trace::sha256_hex(value.data(), value.size()) != item.sha) return 1;
    }
    const std::string abc = "abc";
    if (image_trace::sha256_hex(reinterpret_cast<const unsigned char *>(abc.data()), abc.size()) !=
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad") return 1;
    const std::string million(1000000, 'a');
    if (image_trace::sha256_hex(reinterpret_cast<const unsigned char *>(million.data()), million.size()) !=
        "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0") return 1;
    std::puts("PASS: diagnostic RGB SHA-256 known vectors");
    return 0;
}
