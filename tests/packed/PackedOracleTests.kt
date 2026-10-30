package com.Bilibili_Innocent_Lab.xposedmodule.hook.feature.danmakuwire

import io.github.jichuo1.meguviz.memory.packed.PackedDanmakuReader
import java.io.ByteArrayOutputStream
import java.util.Random

/** Tests compile with the actual hash-locked DanmakuWire and Fields sources. */
private val fields = DanmakuWireFields(1, 2, 3, 4, 5, 6)
private fun varint(value: Long): ByteArray {
    val out=ByteArrayOutputStream();var v=value
    while(true) { var b=(v and 127).toInt();v=v ushr 7;if(v!=0L)b=b or 128;out.write(b);if(v==0L)break }
    return out.toByteArray()
}
private fun number(field: Int, value: Int)=varint(field.toLong() shl 3)+varint(value.toLong())
private fun bytes(field: Int, value: ByteArray)=varint((field.toLong() shl 3) or 2)+varint(value.size.toLong())+value
private fun reply(random: Random, n: Int, s: Int): ByteArray {
    val out=ByteArrayOutputStream()
    out.write(number(31, 777)) // unknown top-level bytes must survive rewrite
    repeat(n) { index ->
        val e=ByteArrayOutputStream()
        val weights=arrayOf<Int?>(null,-1,0,1,10,20,Int.MIN_VALUE,Int.MAX_VALUE)
        weights[random.nextInt(weights.size)]?.let{e.write(number(2,it))}
        if(index%11==0)e.write(number(2,15)) // duplicate weight: last wins
        val colors=arrayOf<Int?>(null,0,60001,1,Int.MIN_VALUE,Int.MAX_VALUE)
        colors[random.nextInt(colors.size)]?.let{e.write(number(3,it))}
        if(index%13==0) { e.write(number(3,1));e.write(number(3,60001)) } // recolor can reject this case
        if(index%3!=0) {
            val text=if(index%5==0)byteArrayOf(0xc3.toByte(),0x28) else "text-$index-中文".toByteArray()
            e.write(bytes(4,text))
        }
        if(index%7==0)e.write(bytes(4,byteArrayOf())) // present empty content
        e.write(bytes(29,byteArrayOf(1,2,3)))
        out.write(bytes(1,e.toByteArray()))
    }
    repeat(s) { index ->
        val e=ByteArrayOutputStream()
        if(index%3!=0)e.write(number(6,if(index%2==0)60001 else 0))
        e.write(number(28,42));out.write(bytes(5,e.toByteArray()))
    }
    return out.toByteArray()
}

private var comparisons=0
private var beforePlusAfter=0L
private var rescanCount=0
private var rejectedRewrites=0
private fun verify(input: ByteArray, threshold: Int?, vip: Boolean, semantic: BooleanArray) {
    val scan=checkNotNull(DanmakuWire.scan(input,fields))
    val flat=DanmakuWire.flatten(scan)
    val materialized=checkNotNull(DanmakuWire.fromFlattened(flat))
    PackedDanmakuReader.borrowImmutable(flat,input).use { reader ->
        check(reader.elementCount()==materialized.elemCount && reader.sourceCount()==materialized.srcCount)
        materialized.elems.forEachIndexed { i,e ->
            check(reader.hasWeight(i)==(e.weight!=null) && reader.weightOr(i,12345)==(e.weight?:12345))
            check(reader.hasColorful(i)==(e.colorful!=null) && reader.colorfulOr(i,12345)==(e.colorful?:12345))
            check(reader.hasContent(i)==e.hasContent && reader.contentStart(i)==e.contentStart && reader.contentLength(i)==e.contentLen)
            val text=if(e.hasContent)String(input,e.contentStart,e.contentLen,Charsets.UTF_8)else ""
            check(reader.contentUtf8(i)==text)
        }
        materialized.srcKinds.forEachIndexed { i,s ->
            check(reader.hasSourceKind(i)==(s!=null) && reader.sourceKindOr(i,12345)==(s?:12345))
        }
        val usable=threshold==null || materialized.elems.any{(it.weight?:0)>0}
        val keep=BooleanArray(materialized.elemCount){i ->
            (threshold==null || !usable || (materialized.elems[i].weight?:threshold)>=threshold) && !semantic[i]
        }
        val recolor=BooleanArray(materialized.elemCount){i ->vip && keep[i] && materialized.elems[i].colorful==60001}
        val keepSrc=BooleanArray(materialized.srcCount){i ->!vip || materialized.srcKinds[i]==null || materialized.srcKinds[i]!=60001}
        val changed=keep.any{!it} || recolor.any{it} || keepSrc.any{!it}
        val decision=reader.decide(threshold!=null,threshold?:0,vip,60001,semantic)
        check(decision.keepElements.contentEquals(keep) && decision.recolorElements.contentEquals(recolor) &&
              decision.keepSources.contentEquals(keepSrc) && decision.changed==changed && decision.usableWeight==usable)
        beforePlusAfter+=scan.elemCount
        if(changed) {
            val original=DanmakuWire.rewrite(input,fields,keep,recolor,keepSrc)
            val packed=DanmakuWire.rewrite(input,fields,decision.keepElements,decision.recolorElements,decision.keepSources)
            check((original==null)==(packed==null))
            if(original==null)rejectedRewrites++
            else {
                check(original.contentEquals(packed!!))
                val rescan=checkNotNull(DanmakuWire.scan(packed,fields));rescanCount++
                check(rescan.elemCount==keep.count{it} && rescan.srcCount==keepSrc.count{it})
                PackedDanmakuReader.copyOf(DanmakuWire.flatten(rescan),packed).use { after ->
                    check(after.elementCount()==rescan.elemCount && after.sourceCount()==rescan.srcCount)
                    var index=0
                    for(i in keep.indices)if(keep[i]) {
                        if(recolor[i])check(after.hasColorful(index) && after.colorfulOr(index,-1)==0)
                        index++
                    }
                    rescan.elems.forEachIndexed{i,e -> check(after.weightOr(i,123)==(e.weight?:123))}
                }
                beforePlusAfter+=rescan.elemCount
            }
        }
        comparisons++
    }
}

fun main() {
    val random=Random(20261030)
    var fixtures=0
    for(n in listOf(0,1,2,3,17,64)) {
        repeat(8) {
            val input=reply(random,n,random.nextInt(9)); fixtures++
            val blocked=BooleanArray(n){random.nextBoolean()}
            for(threshold in listOf<Int?>(null,0,10))for(vip in listOf(false,true))verify(input,threshold,vip,blocked)
        }
    }
    // Exactly MAX_ELEMS/MAX_SRCS is legal for the bounded reader.
    val edge=DanmakuWire.Scan(List(65_536){DanmakuWire.ElemSignal(null,null,-1,0,false)},List(65_536){null})
    PackedDanmakuReader.copyOf(DanmakuWire.flatten(edge),byteArrayOf()).use {
        check(it.elementCount()==65_536 && it.sourceCount()==65_536)
        check(it.decide(false,0,false,0,null).keepElements.all{value->value})
    }
    val invalidReplies=listOf(byteArrayOf(0),byteArrayOf(0x0b),byteArrayOf(0x0c),byteArrayOf(0x0f),byteArrayOf(0x0a,0x7f))
    check(invalidReplies.all{DanmakuWire.scan(it,fields)==null})
    println("{\"suite\":\"real-kotlin-wire-oracle\",\"status\":\"PASS\",\"fixtures\":$fixtures,\"policy_comparisons\":$comparisons,\"rescans\":$rescanCount,\"oracle_rewrite_rejections\":$rejectedRewrites,\"baseline_N_plus_N_prime_constructor_calls_model\":$beforePlusAfter,\"reader_per_element_descriptor_constructions\":0,\"native_jni_execution\":false,\"host_parseFrom_execution\":false}")
}
