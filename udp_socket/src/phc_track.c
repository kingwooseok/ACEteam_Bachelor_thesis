/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include "ace_phc.h"
#include "ace_run_control.h"
#include <signal.h>
#include <sys/timex.h>

/* Five observations every 100 ms. Capture stays in memory until SIGTERM. */
#define BATCH 5
#define INTERVAL_NS 100000000LL
struct observation {
    struct result samples[BATCH];
    unsigned selected;
    int64_t mono, raw;
    long frequency, offset;
    int status;
};
static volatile sig_atomic_t stopping;
static void stop_capture(int signal_number) { (void)signal_number; stopping=1; }
static int64_t ns(struct timespec t) { return (int64_t)t.tv_sec*1000000000LL+t.tv_nsec; }
static unsigned median_sample(struct result *samples)
{
    unsigned order[BATCH],n=0;
    for (unsigned i=0;i<BATCH;i++) if (samples[i].valid) {
        unsigned j=n;
        while (j && (samples[order[j-1]].offset_ns>samples[i].offset_ns ||
               (samples[order[j-1]].offset_ns==samples[i].offset_ns && samples[order[j-1]].span_ns>samples[i].span_ns))) {
            order[j]=order[j-1]; j--;
        }
        order[j]=i; n++;
    }
    return order[n/2];
}
int main(int argc,char **argv)
{
    uint64_t budget_ms;
    if (argc!=6 || strlen(argv[1])>=IFNAMSIZ || ace_parse_u64(argv[5],100,172800000,&budget_ms)) {
        fprintf(stderr,"Usage: phc_track IFACE CSV AUDIT_CSV READY_JSON MAX_MS\n"); return 2;
    }
    size_t capacity=(size_t)(budget_ms/100+3),count=0;
    struct observation *records=calloc(capacity,sizeof(*records));
    if (!records) return 1;
    FILE *csv=ace_open_exclusive_stream(argv[2]),*audit=ace_open_exclusive_stream(argv[3]);
    if (!csv || !audit) { if(csv)fclose(csv); if(audit)fclose(audit); free(records); return 1; }
    struct sigaction action={.sa_handler=stop_capture}; sigemptyset(&action.sa_mask);
    sigaction(SIGTERM,&action,NULL); sigaction(SIGINT,&action,NULL);
    int fd=-1,error=0; unsigned reopens=0; bool ready=false;
    int64_t start=ace_mono_ns(),next=start;
    while (!stopping && count<capacity && ace_mono_ns()-start<(int64_t)budget_ms*1000000LL) {
        char device[64]; struct observation *o=&records[count];
        if (fd<0 && !phc_device_for_interface(argv[1],device,sizeof(device))) fd=open(device,O_RDONLY|O_CLOEXEC);
        memset(o,0,sizeof(*o)); unsigned minimum_span=0;
        if (fd<0 || collect_batch(fd,BATCH,o->samples,&minimum_span)) {
            if(fd>=0)close(fd);
            fd=-1; reopens++;
        } else {
            struct timespec t; struct timex adjustment={0};
            o->selected=median_sample(o->samples);
            if(clock_gettime(CLOCK_MONOTONIC,&t)) { error=1; break; } o->mono=ns(t);
            if(clock_gettime(CLOCK_MONOTONIC_RAW,&t)) { error=1; break; } o->raw=ns(t);
            if(adjtimex(&adjustment)<0) { error=1; break; }
            o->frequency=adjustment.freq; o->offset=adjustment.offset; o->status=adjustment.status;
            count++;
            if(!ready) { if(ace_publish_ready(argv[4],"phc_track")) { error=1; break; } ready=true; }
        }
        next+=INTERVAL_NS;
        /* Missed intervals remain gaps instead of a high-priority catch-up loop. */
        int64_t now=ace_mono_ns(); if(next<now)next=now+INTERVAL_NS;
        struct timespec wake={.tv_sec=next/1000000000LL,.tv_nsec=next%1000000000LL};
        int rc; do { rc=clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&wake,NULL); } while(rc==EINTR&&!stopping);
        if(rc && rc!=EINTR) { error=1; break; }
    }
    if(!stopping || !count)error=1;
    fprintf(csv,"# clock=CLOCK_MONOTONIC offset=phc-minus-monotonic selection=median-offset-observed-sample interval_ms=100\niteration,sample,system_before_ns,phc_ns,system_after_ns,span_ns,offset_ns,selected\n");
    fprintf(audit,"iteration,monotonic_ns,raw_ns,ntp_freq_scaled_ppm,ntp_offset,ntp_status\n");
    for(size_t i=0;i<count;i++) {
        struct observation *o=&records[i];
        fprintf(audit,"%zu,%"PRId64",%"PRId64",%ld,%ld,%d\n",i,o->mono,o->raw,o->frequency,o->offset,o->status);
        for(unsigned j=0;j<BATCH;j++) {
            struct result *r=&o->samples[j]; if(!r->valid)continue;
            fprintf(csv,"%zu,%u,%"PRId64",%"PRId64",%"PRId64",%"PRId64",%"PRId64",%u\n",i,j,r->before_ns,r->phc_ns,r->after_ns,r->span_ns,r->offset_ns,j==o->selected);
        }
    }
    if(ace_finish_stream(csv))error=1;
    if(ace_finish_stream(audit))error=1;
    if(fd>=0)close(fd);
    free(records);
    fprintf(stderr,"samples=%zu reopen_attempts=%u interval_ms=100 exit=%d\n",count,reopens,error);
    return error;
}
