
import numpy as np
import timeit
import scipy.cluster
import scipy.cluster.vq
import math
from numpy.random import randint
from pycuda import driver, compiler, gpuarray
import pycuda.autoinit

def cu_vq(obs, clusters):
    kernel_code_template = """
     #include "float.h" // for FLT_MAX
       // the kernel definition
      __device__ void loadVector(float *target, float* source, int dimensions )
      {
          for( int i = 0; i < dimensions; i++ ) target[i] = source[i];
      }
      __global__ void cu_vq(float *g_idata, float *g_centroids,
        int * cluster, float *min_dist, int numClusters, int numDim, int numPoints) {
        int valindex = blockIdx.x * blockDim.x + threadIdx.x ;
        __shared__ float mean[%(DIMENSIONS)s];
        float minDistance = FLT_MAX;
        int myCentroid = 0;
        if(valindex < numPoints){
          for(int k=0;k<numClusters;k++){
            if(threadIdx.x == 0) loadVector( mean, &g_centroids[k*(numDim)], numDim );
            __syncthreads();
            float distance = 0.0;
            for(int i=0;i<numDim;i++){
              float increased_distance = (g_idata[valindex+i*(numPoints)] -mean[i]);
              distance = distance +(increased_distance * increased_distance);
            }
            if(distance<minDistance) {
              minDistance = distance ;
              myCentroid = k;
            }
          }
          cluster[valindex]=myCentroid;
          min_dist[valindex]=sqrt(minDistance);
        }
      }
    """
    nclusters = clusters.shape[0]
    points = obs.shape[0]
    dimensions = obs.shape[1]
    block_size = 512
    blocks = int(math.ceil(float(points) / block_size))

    kernel_code = kernel_code_template % {
                      'DIMENSIONS': dimensions}
    mod = compiler.SourceModule(kernel_code)

    dataT = obs.T.astype(np.float32).copy()
    clusters = clusters.astype(np.float32)

    cluster = gpuarray.zeros(points, dtype=np.int32)
    min_dist = gpuarray.zeros(points, dtype=np.float32)

    kmeans_kernel = mod.get_function('cu_vq')
    kmeans_kernel(driver.In(dataT),
                  driver.In(clusters),
                  cluster,
                  min_dist,
                  np.int32(nclusters),
                  np.int32(dimensions),
                  np.int32(points),
        grid=(blocks, 1),
        block=(block_size, 1, 1),
    )

    return cluster.get(), min_dist.get()


def _cukmeans(obs, guess, thresh=1e-5):
# https://github.com/scipy/scipy/blob/master/scipy/cluster/vq.py
    code_book = np.array(guess, copy=True)
    avg_dist = []
    diff = thresh + 1.
    while diff > thresh:
        nc = code_book.shape[0]
        #compute membership and distances between obs and code_book
        obs_code, distort = cu_vq(obs, code_book)
        avg_dist.append(np.mean(distort, axis=-1))
        #recalc code_book as centroids of associated obs
        if(diff > thresh):
            has_members = []
            for i in np.arange(nc):
                cell_members = np.compress(np.equal(obs_code, i), obs, 0)
                if cell_members.shape[0] > 0:
                    code_book[i] = np.mean(cell_members, 0)
                    has_members.append(i)
            #remove code_books that didn't have any members
            code_book = np.take(code_book, has_members, 0)
        if len(avg_dist) > 1:
            diff = avg_dist[-2] - avg_dist[-1]
    return code_book, avg_dist[-1]


def cukmeans(obs, k_or_guess, iter=20, thresh=1e-5):
    if int(iter) < 1:
        raise ValueError('iter must be at least 1.')
    if type(k_or_guess) == type(np.array([])):
        guess = k_or_guess
        if guess.size < 1:
            raise ValueError("Asked for 0 cluster ? initial book was %s" % \
                             guess)
        result = _cukmeans(obs, guess, thresh=thresh)
    else:
        #initialize best distance value to a large value
        best_dist = np.inf
        No = obs.shape[0]
        k = k_or_guess
        if k < 1:
            raise ValueError("Asked for 0 cluster ? ")
        for i in range(iter):
            #the intial code book is randomly selected from observations
            guess = np.take(obs, randint(0, No, k), 0)
            book, dist = _cukmeans(obs, guess, thresh=thresh)
            if dist < best_dist:
                best_book = book
                best_dist = dist
        result = best_book, best_dist
    return result