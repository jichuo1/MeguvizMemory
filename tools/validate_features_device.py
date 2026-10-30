#!/usr/bin/env python3
"""Short arm64/ART/framework feature qualification; no performance experiment.

Only a fresh owned directory and the fixed diagnostic APK are written. Existing
APK updates require the same verified certificate; no uninstall, data clearing,
unrelated process termination, or policy changes. ADB timeout leaves remote
state UNKNOWN and stops this run without retry.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import zipfile

from run_arm64_validation import Blocked, Failed, CORE_CASES, digest, parse_last_json, verify_arm64_elf, utc_now
from run_paired_device import PairedRunner
from validate_arm64_consumers_v2 import current_release

ROOT=Path(__file__).resolve().parents[1]
PACKAGE="io.github.jichuo1.meguviz.memory.pressureprobe"
INSTRUMENT=PACKAGE+"/io.github.jichuo1.meguviz.memory.android.PressureInstrumentation"
NATIVE={name:ROOT/"build/features-arm64"/name for name in
        ("mgmem_core_tests","mgmem_c_consumer","mgmem_cpp_consumer","mgmem_import_tests",
         "mgmem_pressure_tests","mgmem_abi10_consumer")}


def require(value,message):
    if not value:raise Blocked(message)


class Features(PairedRunner):
    def __init__(self,args):
        args.phase="features";args.input=args.packed_art_jar
        super().__init__(args,args.out_dir)
        self.protocol={"limits":{"phase_max_wall_seconds":args.overall_timeout}}
        self.cache=ROOT/"build"/("features-device-"+secrets.token_hex(6));self.cache.mkdir()
        self.receipt.update({"scope":"ARM64_ART_ANDROID_FEATURES_FUNCTIONAL_ONLY",
            "native_cases":[],"packed":None,"android_report":None,"state":"NOT_RUN","all_passed":False,
            "source_sha256":digest(Path(__file__)),"qualification":{"performance":"NOT_STARTED",
            "BIL":"NOT_INTEGRATED","MTE_HWASAN_energy":"NOT_QUALIFIED"}})

    def redact(self,value):
        value=super().redact(value)
        if isinstance(value,str):
            value=value.replace(str(ROOT),"<workspace>").replace(ROOT.as_posix(),"<workspace>")
            value=value.replace(str(self.args.adb),"<adb>")
            return re.sub(r"\b[A-Za-z]:[\\/][^\r\n]*","<redacted-local-path>",value)
        return value

    def admission_gate(self,first=False):
        value=current_release(self.args.admission,self.args.serial);sha=digest(self.args.admission)
        if first:
            self.admission_sha=sha
            require(not value.get("explicitly_idle_processes",[]),"this_runner_requires_no_test_process_exemptions")
            self.receipt["admission"]={"state":"RELEASED_FOR_MEMORY","sha256":sha,
                "expires_at_utc":value["expires_at_utc"],"owner_body":"PRIVATE_NOT_COPIED"}
        else:require(sha==self.admission_sha,"admission_changed_during_run")

    def command(self,*args,**kwargs):
        if hasattr(self,"admission_sha"):self.admission_gate()
        kwargs["timeout"]=min(kwargs.get("timeout",20),self.remaining())
        return super().command(*args,**kwargs)

    def checked(self,label,arguments,timeout=20,private=False):
        row=self.command(label,arguments,timeout=timeout,capture_private_inventory=private)
        self.require_command(row,label+"_failed");return row

    def host(self,label,arguments):
        self.admission_gate();self.counter+=1
        result=subprocess.run([str(a) for a in arguments],capture_output=True,timeout=min(30,self.remaining()))
        row={"record_id":self.counter,"name":label,"transport":"HOST_SDK_VALIDATION",
            "exit_code":result.returncode,"timed_out":False,"launch_error":None,
            "stdout_sha256":hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256":hashlib.sha256(result.stderr).hexdigest()}
        self.receipt["commands"].append(row);self.save()
        require(result.returncode==0,label+"_failed")
        return result.stdout.decode("utf-8","replace")

    def cert(self,path,label):
        text=self.host(label,[self.java,"-jar",self.build_tools/"lib/apksigner.jar","verify","--print-certs",path])
        values=re.findall(r"certificate SHA-256 digest:\s*([0-9a-fA-F]{64})",text)
        require(len(values)==1,"exactly_one_APK_signer_required");return values[0].lower()

    def prepare(self):
        self.admission_gate(True)
        built=json.loads(self.args.pressure_build_receipt.read_text(encoding="utf-8"))
        require(built.get("status")=="PASS" and built.get("package_name")==PACKAGE and built.get("abi")=="arm64-v8a","wrong_APK_build_receipt")
        require(re.fullmatch(r"[0-9.]+",built["build_tools"]),"unsafe_build_tools_version")
        self.build_tools=self.args.sdk/"build-tools"/built["build_tools"]
        home=self.args.java_home or (Path(os.environ["JAVA_HOME"]) if os.environ.get("JAVA_HOME") else None)
        self.java=home/"bin"/("java.exe" if os.name=="nt" else "java") if home else shutil.which("java")
        require(self.java and Path(self.java).is_file(),"existing_JDK_required_for_certificate_check")
        artifacts=built["artifacts"]
        for item in artifacts:
            path=(ROOT/item["path"]).resolve();require(path.is_relative_to(ROOT) and path.is_file(),"unsafe_APK_artifact")
            require(digest(path)==item["sha256"],"APK_build_artifact_drift")
        apks=[ROOT/a["path"] for a in artifacts if a["path"].endswith(".apk")]
        require(len(apks)==1,"one_diagnostic_APK_required");self.apk=apks[0]
        self.certificate=built["certificate_sha256"].lower()
        require(self.cert(self.apk,"verify-new-APK-certificate")==self.certificate,"new_APK_certificate_mismatch")
        aapt=self.build_tools/("aapt2.exe" if os.name=="nt" else "aapt2")
        badging=self.host("verify-APK-package",[aapt,"dump","badging",self.apk])
        require(re.findall(r"(?m)^package: name='([^']+)'",badging)==[PACKAGE],"APK_package_identity_mismatch")
        library=ROOT/"install/features-arm64/lib/libmgmem.so"
        jni=[ROOT/a["path"] for a in artifacts if a["path"].endswith("/libmgmem_pressure_jni.so")]
        require(len(jni)==1,"one_JNI_runtime_required")
        with zipfile.ZipFile(self.apk) as archive:
            require(hashlib.sha256(archive.read("lib/arm64-v8a/libmgmem.so")).hexdigest()==digest(library),"APK_Memory_runtime_mismatch")
            require(hashlib.sha256(archive.read("lib/arm64-v8a/libmgmem_pressure_jni.so")).hexdigest()==digest(jni[0]),"APK_JNI_runtime_mismatch")
        self.assets={"libmgmem.so":library,**NATIVE,"packed-reader-tests.jar":self.args.packed_art_jar}
        for name,path in self.assets.items():
            require(path.is_file(),"missing_feature_artifact:"+name)
            if name!="packed-reader-tests.jar":verify_arm64_elf(path)
        self.receipt["artifacts"]=[{"name":name,"bytes":path.stat().st_size,"sha256":digest(path)} for name,path in self.assets.items()]
        self.receipt["pressure_build_receipt_sha256"]=digest(self.args.pressure_build_receipt)
        self.receipt["APK"]={"sha256":digest(self.apk),"certificate_sha256":self.certificate,"package":PACKAGE}

    def stage(self):
        self.checked("create-owned-directory",["shell","mkdir",self.remote]);self.receipt["device_directory_created"]=True
        for item in self.receipt["artifacts"]:
            name=item["name"];self.checked("push-"+name,["push",str(self.assets[name]),self.remote+"/"+name])
            row=self.checked("hash-"+name,["shell","sha256sum",self.remote+"/"+name])
            require(row["stdout"].split()[0]==item["sha256"],"staged_artifact_hash_mismatch");item["device_sha256"]=item["sha256"]
        self.checked("chmod-owned-native",["shell","chmod","700"]+[self.remote+"/"+name for name in NATIVE])

    def native(self):
        cases=[(name,[]) for name in NATIVE]+[("mgmem_import_tests",["--poison-"+mode]) for mode in ("retain","release","alias")]
        for name,arguments in cases:
            label=name+("-"+arguments[0][2:] if arguments else "")
            row=self.checked(label,["shell","LD_LIBRARY_PATH="+self.remote,self.remote+"/"+name]+arguments,self.args.task_timeout)
            result=parse_last_json(row["stdout"]);valid=result.get("status")=="PASS"
            if name in ("mgmem_c_consumer","mgmem_abi10_consumer"):
                valid=result.get("consumer")=="pure_c_abi" and result.get("success") is True and type(result.get("backing_after_close")) is int and result["backing_after_close"]==0
            elif name=="mgmem_core_tests":
                valid=valid and result.get("tests")==11 and result.get("checks")==145033 and {x[5:].strip() for x in row["stdout"].splitlines() if x.startswith("PASS ") }==CORE_CASES
            elif name=="mgmem_cpp_consumer":valid=valid and result.get("cases")==5
            elif name=="mgmem_pressure_tests":valid=valid and result.get("tests")==3
            elif arguments:valid=valid and result.get("suite")=="mgmem-import-poison" and result.get("case")==({"--poison-alias":"provider_alias_external_and_owned"}.get(arguments[0],arguments[0]))
            else:valid=valid and result.get("suite")=="mgmem-import-v1" and result.get("groups")==7
            self.receipt["native_cases"].append({"name":label,"result":result,"success":valid,"cleanup_scope":"INTENTIONAL_POISON_QUARANTINE_PROCESS_EXIT_NOT_NORMAL_ZERO" if arguments else "NORMAL_COMPLETE_CLEANUP"})
            self.save();require(valid,"native_feature_contract_failed:"+label)

    def packed(self):
        row=self.checked("packed-ART-contracts",["shell","CLASSPATH="+self.remote+"/packed-reader-tests.jar","app_process","/system/bin","PackedReaderTests"],self.args.task_timeout)
        result=parse_last_json(row["stdout"]);require(result.get("status")=="PASS" and result.get("cases")==32,"packed_ART_contract_failed")
        self.receipt["packed"]=result;self.save()

    def installed_path(self,label,absent=False):
        row=self.command(label,["shell","pm","path",PACKAGE])
        if absent and row["exit_code"] in (0,1) and not row["stdout"].strip() and not row["stderr"].strip() and not row["timed_out"] and not row["launch_error"]:return None
        self.require_command(row,"diagnostic_package_query_failed")
        paths=re.findall(r"(?m)^package:(/[^\r\n]+\.apk)\r?$",row["stdout"])
        require(paths and all(p.startswith(("/data/app/","/mnt/expand/")) for p in paths),"unexpected_existing_package_path")
        bases=[p for p in paths if p.endswith("/base.apk")];require(len(bases)==1 or len(paths)==1,"ambiguous_base_APK")
        return bases[0] if bases else paths[0]

    def android(self):
        previous=self.installed_path("query-own-existing-APK",True)
        if previous:
            cache=self.cache/"existing-own.apk";self.checked("pull-existing-own-APK",["pull",previous,str(cache)])
            old_cert=self.cert(cache,"verify-existing-APK-certificate")
            require(old_cert==self.certificate,"existing_diagnostic_certificate_differs_no_update_or_uninstall")
            self.receipt["APK"]["prior_certificate_sha256"]=old_cert
        install=self.checked("install-own-diagnostic-APK",["install"]+(["-r"] if previous else [])+[str(self.apk)],60)
        require("Success" in install["stdout"],"APK_install_not_confirmed")
        installed=self.installed_path("query-installed-own-APK")
        copy=self.cache/"installed-own.apk";self.checked("pull-installed-own-APK",["pull",installed,str(copy)])
        require(digest(copy)==digest(self.apk) and self.cert(copy,"verify-installed-APK-certificate")==self.certificate,"installed_APK_identity_mismatch")
        self.receipt["APK"]["installed_sha256"]=digest(copy)
        row=self.checked("instrument-own-pressure-probe",["shell","am","instrument","-w","-r",INSTRUMENT],self.args.instrument_timeout)
        reports=re.findall(r"(?m)^INSTRUMENTATION_RESULT: report_json=(.+)$",row["stdout"])
        require(len(reports)==1,"instrumentation_report_missing_or_ambiguous")
        report=json.loads(reports[0]);self.receipt["android_report"]=report
        require(report.get("suite")=="android-pressure-v1" and report.get("status")=="PASS" and
                type(report.get("final_backing_bytes")) is int and report["final_backing_bytes"]==0 and report.get("actual_low_memory_event") is False and report.get("performance_qualified") is False,"Android_feature_contract_failed")
        for metric,reason in (("rss_kb","rss_unavailable_reason"),("pss_kb","android_memory_unavailable_reason"),("private_dirty_kb","android_memory_unavailable_reason")):
            value=report.get(metric,"MISSING")
            require((value is None and isinstance(report.get(reason),str) and bool(report[reason])) or (type(value) is int and value>=0),"memory_observation_missing_or_invalid:"+metric)
        require(report.get("energy_joules","MISSING") is None and bool(report.get("energy_unavailable_reason")),"energy_unavailable_must_be_explicit")
        ams=report.get("ams_injected_callback_status");require(ams in ("PASS","BLOCKED_UNSUPPORTED_OR_NO_DELIVERY"),"AMS_callback_qualification_missing")
        self.receipt["qualification"].update({"framework_application_dispatch":"PASS_SYNTHETIC_REGISTERED_CALLBACK",
            "AMS_injected_callback":ams,"actual_low_memory_event":"NOT_OBSERVED","RSS_PSS":"OBSERVED_OR_EXPLICIT_UNAVAILABLE_IN_REPORT"})

    def run(self):
        self.prepare();self.occupancy("before",{});metadata={}
        for key,args in {"model":["getprop","ro.product.model"],"api":["getprop","ro.build.version.sdk"],"abi_list":["getprop","ro.product.cpu.abilist"],"page_bytes":["getconf","PAGESIZE"]}.items():
            metadata[key]=self.checked("profile-"+key,["shell"]+args)["stdout"].strip()
        require("arm64-v8a" in metadata["abi_list"].split(",") and metadata["api"].isdigit() and int(metadata["api"])>=27 and metadata["page_bytes"].isdigit(),"unsupported_actual_device_profile")
        metadata["api"],metadata["page_bytes"]=int(metadata["api"]),int(metadata["page_bytes"])
        metadata["boot_id_sha256"]=self.boot_hash("boot-before");self.receipt["device"]=metadata
        self.stage();self.native();self.occupancy("after-native",{});self.packed();self.android()
        self.occupancy("after",{});require(self.boot_hash("boot-after")==metadata["boot_id_sha256"],"device_boot_changed")
        require(len(self.receipt["native_cases"])==9,"native_case_count_incomplete")
        self.receipt.update({"state":"PASS","all_passed":True,"performance_qualified":False})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("adb","admission","out-dir","packed-art-jar","pressure-build-receipt","sdk"):parser.add_argument("--"+name,type=Path,required=True)
    parser.add_argument("--serial",required=True);parser.add_argument("--java-home",type=Path)
    parser.add_argument("--overall-timeout",type=int,default=600);parser.add_argument("--task-timeout",type=int,default=30);parser.add_argument("--instrument-timeout",type=int,default=120)
    args=parser.parse_args();require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",args.serial),"invalid_serial")
    require(1<=args.task_timeout<=60 and 1<=args.instrument_timeout<=180 and 60<=args.overall_timeout<=1200,"invalid_time_caps")
    args.out_dir=args.out_dir.resolve();require(args.out_dir.is_relative_to(ROOT) and not args.out_dir.exists(),"fresh_output_inside_workspace_required");args.out_dir.mkdir(parents=True)
    runner=Features(args);code=1
    try:runner.run();code=0
    except Blocked as error:runner.receipt.update({"state":"BLOCKED","reason":str(error)});code=2
    except (Failed,RuntimeError,OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as error:runner.receipt.update({"state":"FAILED","reason":type(error).__name__+":"+str(error)})
    except KeyboardInterrupt:runner.receipt.update({"state":"INTERRUPTED_INCOMPLETE","reason":"operator_interrupt_remote_state_unknown"})
    finally:runner.receipt["completed_at_utc"]=utc_now();runner.save(force=True)
    print(json.dumps({"state":runner.receipt["state"],"native_cases":len(runner.receipt["native_cases"]),"packed":runner.receipt["packed"],"AMS":runner.receipt["qualification"].get("AMS_injected_callback"),"performance_qualified":False}));return code


if __name__=="__main__":raise SystemExit(main())
