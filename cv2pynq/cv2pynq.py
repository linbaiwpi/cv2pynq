import os
import numpy as np
from pynq import Overlay, PL, MMIO
from pynq import DefaultIP, DefaultHierarchy
from pynq import Xlnk
from pynq.xlnk import ContiguousArray
from pynq.lib import DMA
from cffi import FFI
import cv2

CV2PYNQ_ROOT_DIR = os.path.dirname(os.path.realpath(__file__))
CV2PYNQ_BIT_DIR = os.path.join(CV2PYNQ_ROOT_DIR, 'bitstreams')

class cv2pynq():
    MAX_WIDTH  = 1920
    MAX_HEIGHT = 1080
    def __init__(self, load_overlay=True):
        self.bitstream_name = None
        self.bitstream_name = "cv2pynq02.bit"
        self.bitstream_path = os.path.join(CV2PYNQ_BIT_DIR, self.bitstream_name)
        #if PL.bitfile_name != self.bitstream_path:#todo ol
        #if load_overlay:
        self.ol = Overlay(self.bitstream_path)
        self.ol.download()
        self.ol.reset() # todo
        print("downloaded overlay", self.bitstream_path)
        #else:
        #    raise RuntimeError("Incorrect Overlay loaded")
        self.xlnk = Xlnk()
        self.partitions = 10 #split the cma into partitions for pipelined transfer
        self.cmaPartitionLen = self.MAX_HEIGHT*self.MAX_WIDTH/self.partitions
        self.listOfcma = [self.xlnk.cma_array(shape=(int(self.MAX_HEIGHT/self.partitions),self.MAX_WIDTH), dtype=np.uint8) for i in range(self.partitions)]
        self.img_filters = self.ol.image_filters
        self.dmaOut = self.img_filters.axi_dma_0.sendchannel 
        self.dmaIn =  self.img_filters.axi_dma_0.recvchannel 
        self.dmaOut.stop()#todo
        self.dmaIn.stop()
        self.dmaIn.start()
        self.dmaOut.start()
        self.filter2DType = -1  # filter types: SobelX=0, SobelY=1, ScharrX=2, ScharrY=3, Laplacian=4
        self.filter2DfType = -1 # filter types: blur=0, GaussianBlur=1
        self.ffi = FFI()
        self.f2D = self.img_filters.filter2D_hls_0
        self.f2D.reset()
        self.f2D_f = self.img_filters.filter2D_f_0
        self.f2D_f.reset()
        self.erodeIP = self.img_filters.erode_hls_0
        self.erodeIP.reset()
        self.dilateIP = self.img_filters.dilate_hls_0
        self.dilateIP.reset()
        self.cmaBuffer_0 = self.xlnk.cma_array(shape=(self.MAX_HEIGHT,self.MAX_WIDTH), dtype=np.uint8)
        self.cmaBuffer0 =  self.cmaBuffer_0.view(self.ContiguousArrayCv2pynq)
        self.cmaBuffer0.init(self.cmaBuffer_0)
        self.cmaBuffer_1 = self.xlnk.cma_array(shape=(self.MAX_HEIGHT,self.MAX_WIDTH), dtype=np.uint8)
        self.cmaBuffer1 =  self.cmaBuffer_1.view(self.ContiguousArrayCv2pynq)
        self.cmaBuffer1.init(self.cmaBuffer_1)
        self.cmaBuffer_2 = self.xlnk.cma_array(shape=(self.MAX_HEIGHT,self.MAX_WIDTH), dtype=np.uint8)
        self.cmaBuffer2 =  self.cmaBuffer_2.view(self.ContiguousArrayCv2pynq)
        self.cmaBuffer2.init(self.cmaBuffer_2)

    #def __del__(self):
    #    self.dmaOut.stop()
    #    self.dmaIn.stop()
    #    print("_del_")

    def filter2D(self, src, dst):
        self.img_filters.select_filter(1)
        self.f2D.start()
        if dst is None :
            ret = self.xlnk.cma_array(src.shape, dtype=np.uint8)
        elif hasattr(src, 'physical_address') and hasattr(dst, 'physical_address') :
            self.dmaIn.transfer(dst)
            self.dmaOut.transfer(src)
            self.dmaIn.wait()
            return dst
        if hasattr(src, 'physical_address') :
            self.dmaIn.transfer(ret)
            self.dmaOut.transfer(src)
            self.dmaIn.wait()
        else:#pipeline the copy to continuous memory and filter calculation in hardware
            chunks = int(src.nbytes / (self.cmaPartitionLen) )
            pointerCma = self.ffi.cast("uint8_t *",  self.ffi.from_buffer(self.listOfcma[0]))
            pointerToImage = self.ffi.cast("uint8_t *", self.ffi.from_buffer(src))
            if chunks > 0:
                self.ffi.memmove(pointerCma, pointerToImage, self.listOfcma[0].nbytes)
                self.dmaIn.transfer(ret)
            for i in range(1,chunks):
                while not self.dmaOut.idle and not self.dmaOut._first_transfer:
                    pass 
                self.dmaOut.transfer(self.listOfcma[i-1])
                pointerCma = self.ffi.cast("uint8_t *",  self.ffi.from_buffer(self.listOfcma[i]))
                self.ffi.memmove(pointerCma, pointerToImage+i*self.listOfcma[i].nbytes, self.listOfcma[i].nbytes)
            if chunks > 0:
                while not self.dmaOut.idle and not self.dmaOut._first_transfer:
                    pass 
                self.dmaOut.transfer(self.listOfcma[chunks-1])
            if(src.nbytes % self.cmaPartitionLen != 0):#cleanup code - handle rest of image
                rest = self.xlnk.cma_array(shape=(int(src.nbytes-chunks*self.cmaPartitionLen),1), dtype=np.uint8)
                pointerCma = self.ffi.cast("uint8_t *",  self.ffi.from_buffer(rest))
                self.ffi.memmove(pointerCma, pointerToImage+int(chunks*self.cmaPartitionLen), rest.nbytes)
                while not self.dmaOut.idle and not self.dmaOut._first_transfer:
                    pass 
                self.dmaOut.transfer(rest)
            self.dmaIn.wait()
        return ret

    def Sobel(self,src, ddepth, dx, dy, dst):
        self.f2D.rows = src.shape[0]
        self.f2D.columns = src.shape[1]
        self.f2D.channels = 1
        if (dx == 1) and (dy == 0) :
            if self.filter2DType != 0 :
                self.filter2DType = 0
                self.f2D.r1 = 0x000100ff #[-1  0  1]
                self.f2D.r2 = 0x000200fe #[-2  0  2]
                self.f2D.r3 = 0x000100ff #[-1  0  1]
        elif (dx == 0) and (dy == 1) :
            if self.filter2DType != 1 :
                self.filter2DType = 1
                self.f2D.r1 = 0x00fffeff #[-1 -2 -1]
                self.f2D.r2 = 0x00000000 #[ 0  0  0]
                self.f2D.r3 = 0x00010201 #[ 1  2  1]
        else:
            raise RuntimeError("Incorrect dx dy configuration")  
        return self.filter2D(src, dst)

    def Scharr(self,src, ddepth, dx, dy, dst):
        self.f2D.rows = src.shape[0]
        self.f2D.columns = src.shape[1]
        self.f2D.channels = 1
        if (dx == 1) and (dy == 0) :
            if self.filter2DType != 2 :
                self.filter2DType = 2
                self.f2D.r1 = 0x000300fd #[-3  0  3]
                self.f2D.r2 = 0x000a00f6 #[-10 0 10]
                self.f2D.r3 = 0x000300fd #[-3  0  3]
        elif (dx == 0) and (dy == 1) :
            if self.filter2DType != 3 :
                self.filter2DType = 3
                self.f2D.r1 = 0x00fdf6fd #[-3 -10 -3]
                self.f2D.r2 = 0x00000000 #[ 0   0  0]
                self.f2D.r3 = 0x00030a03 #[ 3  10  3]
        else:
            raise RuntimeError("Incorrect dx dy configuration")  
        return self.filter2D(src, dst)

    def Laplacian(self,src, ddepth, dst):
        self.f2D.rows = src.shape[0]
        self.f2D.columns = src.shape[1]
        self.f2D.channels = 1
        if (self.filter2DType != 4)  :
            self.filter2DType = 4 # "Laplacian"
            self.f2D.r1 = 0x00000100 #[ 0  1  0]
            self.f2D.r2 = 0x0001fc01 #[ 1 -4  1]
            self.f2D.r3 = 0x00000100 #[ 0  1  0] 
        return self.filter2D(src, dst)
    
    def filter2D_f(self, src, dst):
        self.img_filters.select_filter(2)
        #f2D = self.img_filters.filter2D_f_0
        self.f2D_f.start()
        if dst is None :
            ret = self.xlnk.cma_array(src.shape, dtype=np.uint8)
        elif hasattr(src, 'physical_address') and hasattr(dst, 'physical_address') :
            self.dmaIn.transfer(dst)
            self.dmaOut.transfer(src)
            self.dmaIn.wait()
            return dst
        if hasattr(src, 'physical_address') :
            self.dmaIn.transfer(ret)
            self.dmaOut.transfer(src)
            self.dmaIn.wait()
        else:#pipeline the copy to continuous memory and filter calculation in hardware
            chunks = int(src.nbytes / (self.cmaPartitionLen) )
            pointerCma = self.ffi.cast("uint8_t *",  self.ffi.from_buffer(self.listOfcma[0]))
            pointerToImage = self.ffi.cast("uint8_t *", self.ffi.from_buffer(src))
            if chunks > 0:
                self.ffi.memmove(pointerCma, pointerToImage, self.listOfcma[0].nbytes)
                self.dmaIn.transfer(ret)
            for i in range(1,chunks):
                while not self.dmaOut.idle and not self.dmaOut._first_transfer:
                    pass 
                self.dmaOut.transfer(self.listOfcma[i-1])
                pointerCma = self.ffi.cast("uint8_t *",  self.ffi.from_buffer(self.listOfcma[i]))
                self.ffi.memmove(pointerCma, pointerToImage+i*self.listOfcma[i].nbytes, self.listOfcma[i].nbytes)
            if chunks > 0:
                while not self.dmaOut.idle and not self.dmaOut._first_transfer:
                    pass 
                self.dmaOut.transfer(self.listOfcma[chunks-1])
            if(src.nbytes % self.cmaPartitionLen != 0):#cleanup code - handle rest of image
                rest = self.xlnk.cma_array(shape=(int(src.nbytes-chunks*self.cmaPartitionLen),1), dtype=np.uint8)
                pointerCma = self.ffi.cast("uint8_t *",  self.ffi.from_buffer(rest))
                self.ffi.memmove(pointerCma, pointerToImage+int(chunks*self.cmaPartitionLen), rest.nbytes)
                while not self.dmaOut.idle and not self.dmaOut._first_transfer:
                    pass 
                self.dmaOut.transfer(rest)
            self.dmaIn.wait()
        return ret

    def floatToFixed(self, f, total_bits, fract_bits):
        """convert float f to a signed fixed point with #total_bits and #frac_bits after the point"""
        fix = int((abs(f) * (1 << fract_bits)))
        if(f < 0):
            fix += 1 << total_bits-1
        return fix

    def blur(self,src, ksize, dst):
        self.f2D_f.rows = src.shape[0]
        self.f2D_f.columns = src.shape[1]
        if (self.filter2DfType != 0)  :
            self.filter2DfType = 0 #blur
            mean = self.floatToFixed(1/9, cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r11 = mean
            self.f2D_f.r12 = mean
            self.f2D_f.r13 = mean
            self.f2D_f.r21 = mean
            self.f2D_f.r22 = mean
            self.f2D_f.r23 = mean
            self.f2D_f.r31 = mean 
            self.f2D_f.r32 = mean
            self.f2D_f.r33 = mean
        return self.filter2D_f(src, dst)
    
    def GaussianBlur(self, src, ksize, sigmaX, sigmaY, dst):
        self.f2D_f.rows = src.shape[0]
        self.f2D_f.columns = src.shape[1]
        if (self.filter2DfType != 1)  :
            self.filter2DfType = 1 #GaussianBlur
            if(sigmaX <= 0):
                sigmaX = 0.3*((ksize[0]-1)*0.5 - 1) + 0.8
            if(sigmaY <= 0):
                sigmaY = sigmaX
            kX = cv2.getGaussianKernel(3,sigmaX,ktype=cv2.CV_32F) #kernel X
            kY = cv2.getGaussianKernel(3,sigmaY,ktype=cv2.CV_32F) #kernel Y
            self.f2D_f.r11 = self.floatToFixed(kY[0]*kX[0], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r12 = self.floatToFixed(kY[0]*kX[1], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r13 = self.floatToFixed(kY[0]*kX[2], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r21 = self.floatToFixed(kY[1]*kX[0], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r22 = self.floatToFixed(kY[1]*kX[1], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r23 = self.floatToFixed(kY[1]*kX[2], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r31 = self.floatToFixed(kY[2]*kX[0], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r32 = self.floatToFixed(kY[2]*kX[1], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
            self.f2D_f.r33 = self.floatToFixed(kY[2]*kX[2], cv2pynqDriverFilter2D_f.K_FP_W, cv2pynqDriverFilter2D_f.K_FP_F)
        return self.filter2D_f(src, dst)

    def erode(self, src, kernel, dst, iterations, mode):
        self.img_filters.select_filter(3)
        return erodeDilateKernel(src, kernel, dst, iterations, mode, self.erodeIP)
    
    def dilate(self, src, kernel, dst, iterations, mode):
        self.img_filters.select_filter(4)
        return erodeDilateKernel(src, kernel, dst, iterations, mode, self.dilateIP)
    
    def erodeDilateKernel(self, src, kernel, dst, iterations, mode, filter)
        filter.mode = mode
        filter.rows = src.shape[0]
        filter.columns = src.shape[1]
        if hasattr(src, 'physical_address') and hasattr(dst, 'physical_address') :
            filter.start()
            if iterations > 1:
                self.dmaIn.transfer(self.cmaBuffer1)
            else:
                self.dmaIn.transfer(dst)
            self.dmaOut.transfer(src)
            self.dmaIn.wait()
            self.cmaBuffer2.nbytes = src.nbytes #buffer = self.xlnk.cma_array(src.shape, dtype=np.uint8)   
            for i in range(2, iterations+1):
                filter.start()
                if i % 2 == 0:
                    self.dmaIn.transfer(self.cmaBuffer2)
                    if i != iterations: #avoid copy after last iteration
                        self.dmaOut.transfer(self.cmaBuffer1)
                    else:
                        self.dmaOut.transfer(dst)
                else: 
                    self.dmaIn.transfer(self.cmaBuffer1)
                    if i != iterations:
                        self.dmaOut.transfer(self.cmaBuffer2)
                    else:
                        self.dmaOut.transfer(dst) 
                self.dmaIn.wait()
            return dst
        self.cmaBuffer0.nbytes = src.nbytes
        self.cmaBuffer1.nbytes = src.nbytes
        filter.start()
        self.dmaIn.transfer(self.cmaBuffer1)
        if hasattr(src, 'physical_address') :
            self.dmaOut.transfer(src)        
        else:
            self.copyNto(self.cmaBuffer0,src,src.nbytes) #np.copyto(srcBuffer,src)
            self.dmaOut.transfer(self.cmaBuffer0)          
        self.dmaIn.wait()
        self.cmaBuffer2.nbytes = src.nbytes #buffer = self.xlnk.cma_array(src.shape, dtype=np.uint8)    
        for i in range(2, iterations+1):
            filter.start()
            if i % 2 == 0:
                self.dmaIn.transfer(self.cmaBuffer2)
                self.dmaOut.transfer(self.cmaBuffer1)
            else: 
                self.dmaIn.transfer(self.cmaBuffer1)
                self.dmaOut.transfer(self.cmaBuffer2)
            self.dmaIn.wait()
        ret = np.ndarray(src.shape,src.dtype)
        if iterations % 2 == 1:
            self.copyNto(ret,self.cmaBuffer1,ret.nbytes)
        else:
            self.copyNto(ret,self.cmaBuffer2,ret.nbytes)
        return ret
    
    def copyNto(self,dst,src,N):
        dstPtr = self.ffi.cast("uint8_t *", self.ffi.from_buffer(dst))
        srcPtr = self.ffi.cast("uint8_t *", self.ffi.from_buffer(src))
        self.ffi.memmove(dstPtr, srcPtr, N)

    class ContiguousArrayCv2pynq(ContiguousArray):
        def init(self,cmaArray):
            self._nbytes = cmaArray.nbytes
            self.physical_address = cmaArray.physical_address
            self.cacheable = cmaArray.cacheable
        # overwrite access to nbytes with own function
        @property
        def nbytes(self):
            return self._nbytes

        @nbytes.setter
        def nbytes(self, value):
            self._nbytes = value


class cv2pynqDiverImageFilters(DefaultHierarchy):
    def __init__(self, description):
        super().__init__(description)
        self.intc1 = MMIO(0x43C10000, 0x10000)#get axis_interconnect_1
        self.intc2 = MMIO(0x43C20000, 0x10000)#get axis_interconnect_2
        self.filter = 1
        self.intc1.write(0x40 + 0*4, 0x80000000)#disable master0
        self.intc1.write(0x40 + 1*4, 0x00000000)#select slave0 for master0
        self.intc1.write(0x40 + 2*4, 0x80000000)#disable master2
        self.intc1.write(0x40 + 3*4, 0x80000000)#disable master3
        self.intc1.write(0x40 + 4*4, 0x80000000)#disable master4  
        self.intc2.write(0x40, self.filter)#select slave# for master0 
        self.intc1.write(0x00, 0x2)#reset interconnect 1
        self.intc2.write(0x00, 0x2)#reset interconnect 2

    @staticmethod
    def checkhierarchy(description):
        if 'axi_dma_0' in description['ip'] \
           and 'axis_interconnect_1' in description['ip'] \
           and 'axis_interconnect_2' in description['ip'] \
           and 'canny_edge_0' in description['ip'] \
           and 'filter2D_hls_0' in description['ip'] \
           and 'filter2D_f_0' in description['ip'] \
           and 'erode_hls_0' in description['ip'] \
           and 'dilate_hls_0' in description['ip'] :
            return True
        return False
    
    def select_filter(self, filter):
        if not self.filter == filter:
            self.intc1.write(0x40 + self.filter*4, 0x80000000)#disable old master
            self.intc1.write(0x40 + filter*4, 0x00000000)#select slave0 for new master
            self.intc2.write(0x40, filter)#select new slave for master0
            self.intc1.write(0x00, 0x2)#reset interconnect 1
            self.intc2.write(0x00, 0x2)#reset interconnect 2
            self.filter = filter
        

class cv2pynqDriverFilter2D(DefaultIP):
    def __init__(self, description):
        super().__init__(description=description)
        self.reset()
        
    bindto = ['xilinx.com:hls:filter2D_hls:1.0']

    def start(self):
        self.write(0x00, 0x01)

    def auto_restart(self):
        self.write(0x00, 0x81)

    def reset(self):
        self.rows_value = -1
        self.rows = 0
        self.columns_value = -1
        self.columns = 0
        self.channels_value = -1
        self.channels = 1 
        self.mode_value = -1
        self.mode = 0  
        self.r1_value = -1  
        self.r1 = 0
        self.r2_value = -1
        self.r2 = 0
        self.r3_value = -1
        self.r3 = 0
 
    @property
    def rows(self):
        return self.read(0x14)
    @rows.setter
    def rows(self, value):
        if not self.rows_value == value:
            self.write(0x14, value)
            self.rows_value = value

    @property
    def columns(self):
        return self.read(0x1c)
    @columns.setter
    def columns(self, value):
        if not self.columns_value == value:
            self.write(0x1c, value)
            self.columns_value = value

    @property
    def channels(self):
        return self.read(0x24)
    @channels.setter
    def channels(self, value):
        if not self.channels_value == value:
            self.write(0x24, value)
            self.channels_value = value
        
    @property
    def mode(self):
        return self.read(0x2c)
    @mode.setter
    def mode(self, value):
        if not self.mode_value == value:
            self.write(0x2c, value)
            self.mode_value = value    
    
    @property
    def r1(self):
        return self.read(0x34)
    @r1.setter
    def r1(self, value):
        if not self.r1_value == value:
            self.write(0x34, value)
            self.mode_value = value         
    
    @property
    def r2(self):
        return self.read(0x3c)
    @r2.setter
    def r2(self, value):
        if not self.r2_value == value:
            self.write(0x3c, value)
            self.mode_value = value

    @property
    def r3(self):
        return self.read(0x44)
    @r3.setter
    def r3(self, value):
        if not self.r3_value == value:
            self.write(0x44, value)
            self.mode_value = value


class cv2pynqDriverFilter2D_f(DefaultIP):
    def __init__(self, description):
        super().__init__(description=description)
        self.reset()
        
    bindto = ['xilinx.com:hls:filter2D_f:1.0']
    K_FP_W = 25 #kernel fixed point: length in bits
    K_FP_F = 23 #kernel fixed point: The number of bits used to represent the number of bits behind the decimal point
    def start(self):
        self.write(0x00, 0x01)

    def auto_restart(self):
        self.write(0x00, 0x81)

    def reset(self):
        self.rows_value = -1
        self.rows = 0
        self.columns_value = -1
        self.columns = 0
        self.channels_value = -1
        self.channels = 1 
        self.mode_value = -1
        self.mode = 0  
        self.r11_value = -1  
        self.r11 = 0
        self.r12_value = -1
        self.r12 = 0
        self.r13_value = -1
        self.r13 = 0
        self.r21_value = -1
        self.r21 = 0
        self.r22_value = -1
        self.r22 = 0
        self.r23_value = -1
        self.r23 = 0
        self.r31_value = -1
        self.r31 = 0
        self.r32_value = -1
        self.r32 = 0
        self.r33_value = -1
        self.r33 = 0   

    @property
    def rows(self):
        return self.read(0x14)
    @rows.setter
    def rows(self, value):
        if not self.rows_value == value:
            self.write(0x14, value)
            self.rows_value = value

    @property
    def columns(self):
        return self.read(0x1c)
    @columns.setter
    def columns(self, value):
        if not self.columns_value == value:
            self.write(0x1c, value)
            self.columns_value = value

    @property
    def channels(self):
        return self.read(0x24)
    @channels.setter
    def channels(self, value):
        if not self.channels_value == value:
            self.write(0x24, value)
            self.channels_value = value
        
    @property
    def mode(self):
        return self.read(0x2c)
    @mode.setter
    def mode(self, value):
        if not self.mode_value == value:
            self.write(0x2c, value)
            self.mode_value = value    
    
    @property
    def r11(self):
        return self.read(0x34)
    @r11.setter
    def r11(self, value):
        if not self.r11_value == value:
            self.write(0x34, value)
            self.mode_value = value         
    
    @property
    def r12(self):
        return self.read(0x3c)
    @r12.setter
    def r12(self, value):
        if not self.r12_value == value:
            self.write(0x3c, value)
            self.mode_value = value

    @property
    def r13(self):
        return self.read(0x44)
    @r13.setter
    def r13(self, value):
        if not self.r13_value == value:
            self.write(0x44, value)
            self.mode_value = value

    @property
    def r21(self):
        return self.read(0x4c)
    @r21.setter
    def r21(self, value):
        if not self.r21_value == value:
            self.write(0x4c, value)
            self.mode_value = value         
    
    @property
    def r22(self):
        return self.read(0x54)
    @r22.setter
    def r22(self, value):
        if not self.r22_value == value:
            self.write(0x54, value)
            self.mode_value = value

    @property
    def r23(self):
        return self.read(0x5c)
    @r23.setter
    def r23(self, value):
        if not self.r23_value == value:
            self.write(0x5c, value)
            self.mode_value = value

    @property
    def r31(self):
        return self.read(0x64)
    @r31.setter
    def r31(self, value):
        if not self.r31_value == value:
            self.write(0x64, value)
            self.mode_value = value         
    
    @property
    def r32(self):
        return self.read(0x6c)
    @r32.setter
    def r32(self, value):
        if not self.r32_value == value:
            self.write(0x6c, value)
            self.mode_value = value

    @property
    def r33(self):
        return self.read(0x74)
    @r33.setter
    def r33(self, value):
        if not self.r33_value == value:
            self.write(0x74, value)
            self.mode_value = value

class cv2pynqDriverCanny(DefaultIP):
    def __init__(self, description):
        super().__init__(description=description)
        self.reset()
        
    bindto = ['xilinx.com:hls:canny_edge:1.0']

    def start(self):
        self.write(0x00, 0x01)

    def auto_restart(self):
        self.write(0x00, 0x81)

    def reset(self):
        self.rows_value = -1
        self.rows = 0
        self.columns_value = -1
        self.columns = 0
        self.threshold1_value = -1
        self.threshold1 = 0
        self.threshold2_value = -1
        self.threshold2 = 0
 
    @property
    def rows(self):
        return self.read(0x14)
    @rows.setter
    def rows(self, value):
        if not self.rows_value == value:
            self.write(0x14, value)
            self.rows_value = value

    @property
    def columns(self):
        return self.read(0x1c)
    @columns.setter
    def columns(self, value):
        if not self.columns_value == value:
            self.write(0x1c, value)
            self.columns_value = value

    @property
    def threshold1(self):
        return self.read(0x24)
    @threshold1.setter
    def threshold1(self, value):
        if not self.threshold1_value == value:
            self.write(0x24, value)
            self.threshold1_value = value
        
    @property
    def threshold2(self):
        return self.read(0x2c)
    @threshold2.setter
    def threshold2(self, value):
        if not self.threshold2_value == value:
            self.write(0x2c, value)
            self.threshold2_value = value    
    
class cv2pynqDriverErode(DefaultIP):
    def __init__(self, description):
        super().__init__(description=description)
        self.reset()
        
    bindto = ['xilinx.com:hls:erode_hls:1.0']

    def start(self):
        self.write(0x00, 0x01)
    
    def reset(self):
        self.rows_value = -1
        self.rows = 0
        self.columns_value = -1
        self.columns = 0
        self.channels_value = -1
        self.channels = 1
        self.mode_value = -1
        self.mode = 0
        
    @property
    def rows(self):
        return self.read(0x14)
    @rows.setter
    def rows(self, value):
        if not self.rows_value == value:
            self.write(0x14, value)
            self.rows_value = value

    @property
    def columns(self):
        return self.read(0x1c)
    @columns.setter
    def columns(self, value):
        if not self.columns_value == value:
            self.write(0x1c, value)
            self.columns_value = value
    
    @property
    def channels(self):
        return self.read(0x24)
    @channels.setter
    def channels(self, value):
        if not self.channels_value == value:
            self.write(0x24, value)
            self.channels_value = value

    @property
    def mode(self):
        return self.read(0x2c)
    @mode.setter
    def mode(self, value):
        if not self.mode_value == value:
            self.write(0x2c, value)
            self.mode_value = value

class cv2pynqDriverDilate(DefaultIP):
    def __init__(self, description):
        super().__init__(description=description)
        self.reset()
        
    bindto = ['xilinx.com:hls:dilate_hls:1.0']

    def start(self):
        self.write(0x00, 0x01)
    
    def reset(self):
        self.rows_value = -1
        self.rows = 0
        self.columns_value = -1
        self.columns = 0
        self.channels_value = -1
        self.channels = 1
        self.mode_value = -1
        self.mode = 0
        
    @property
    def rows(self):
        return self.read(0x14)
    @rows.setter
    def rows(self, value):
        if not self.rows_value == value:
            self.write(0x14, value)
            self.rows_value = value

    @property
    def columns(self):
        return self.read(0x1c)
    @columns.setter
    def columns(self, value):
        if not self.columns_value == value:
            self.write(0x1c, value)
            self.columns_value = value
    
    @property
    def channels(self):
        return self.read(0x24)
    @channels.setter
    def channels(self, value):
        if not self.channels_value == value:
            self.write(0x24, value)
            self.channels_value = value

    @property
    def mode(self):
        return self.read(0x2c)
    @mode.setter
    def mode(self, value):
        if not self.mode_value == value:
            self.write(0x2c, value)
            self.mode_value = value